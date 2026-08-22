"""Reusable TTS playback telemetry mixin for Dotty's custom providers."""

from __future__ import annotations

import asyncio
import queue
import time

from core.handle.reportHandle import enqueue_tts_report
from core.handle.sendAudioHandle import sendAudioMessage, _wait_for_audio_completion
from core.providers.tts.dto.dto import SentenceType
from core.utils.activity_telemetry import emit_turn
from core.utils.output_counter import add_device_output


class ActivityPlaybackMixin:
    """Preserve upstream playback behaviour while adding correlation hooks."""

    def activity_bind_sentence(self, sentence_id: str | None) -> None:
        if not sentence_id:
            return
        if not hasattr(self, "_dotty_activity_sentences"):
            self._dotty_activity_sentences = {}
        pending = getattr(self.conn, "_dotty_pending_activity_turns", None)
        if pending is not None:
            try:
                turn_id, started = pending.popleft()
            except IndexError:
                turn_id, started = None, time.time()
        else:
            turn_id = getattr(self.conn, "_dotty_active_turn_id", None)
            started = float(getattr(self.conn, "_dotty_activity_start_ts", time.time()))
        self._dotty_activity_sentences[sentence_id] = {
            "turn_id": turn_id,
            "started": started,
            "tts_started": None,
            "playback_started": False,
        }

    def activity_abort_sentence(self, sentence_id: str | None) -> None:
        ctx = self._activity_ctx(sentence_id)
        if not ctx:
            return
        emit_turn(
            "aborted", ctx["turn_id"], source="tts",
            session_id=getattr(self.conn, "session_id", None),
            device_id=getattr(self.conn, "device_id", None),
        )
        self._dotty_activity_sentences.pop(sentence_id, None)

    def _activity_ctx(self, sentence_id: str | None):
        mapping = getattr(self, "_dotty_activity_sentences", {})
        return mapping.get(sentence_id) if sentence_id else None

    def activity_tts_started(self, sentence_id: str | None) -> None:
        ctx = self._activity_ctx(sentence_id)
        if not ctx or ctx["tts_started"] is not None:
            return
        ctx["tts_started"] = time.time()
        emit_turn(
            "tts_started", ctx["turn_id"], source="tts",
            session_id=getattr(self.conn, "session_id", None),
            device_id=getattr(self.conn, "device_id", None),
        )

    def activity_tts_failed(self, sentence_id: str | None, error: object) -> None:
        ctx = self._activity_ctx(sentence_id)
        if not ctx:
            return
        ctx["terminal"] = "failed"
        emit_turn(
            "failed", ctx["turn_id"], source="tts",
            session_id=getattr(self.conn, "session_id", None),
            device_id=getattr(self.conn, "device_id", None), error=str(error),
        )

    async def _activity_send_audio(
        self, sentence_type, audio_datas, text, sentence_id,
    ) -> None:
        await sendAudioMessage(
            self.conn, sentence_type, audio_datas, text, sentence_id,
        )
        if sentence_type == SentenceType.LAST:
            # v0.9.3's rate controller sends buffered frames in the background.
            # Wait for that queue plus its prebuffer window before declaring the
            # turn complete, matching the upstream abort/stop lifecycle.
            await _wait_for_audio_completion(self.conn)

    def _audio_play_priority_thread(self):
        enqueue_text = None
        enqueue_audio = []
        while not self.conn.stop_event.is_set():
            text = None
            try:
                try:
                    item = self.tts_audio_queue.get(timeout=0.1)
                    if len(item) == 4:
                        sentence_type, audio_datas, text, sentence_id = item
                    else:
                        sentence_type, audio_datas, text = item
                        sentence_id = getattr(self, "current_sentence_id", None)
                except queue.Empty:
                    if self.conn.stop_event.is_set():
                        break
                    continue
                if self.conn.client_abort:
                    ctx = self._activity_ctx(sentence_id)
                    if ctx:
                        emit_turn(
                            "aborted", ctx["turn_id"], source="tts",
                            session_id=getattr(self.conn, "session_id", None),
                            device_id=getattr(self.conn, "device_id", None),
                        )
                        self._dotty_activity_sentences.pop(sentence_id, None)
                    enqueue_text, enqueue_audio = None, []
                    continue
                if sentence_type is not SentenceType.MIDDLE:
                    if self.report_on_last:
                        if text:
                            enqueue_text = text
                        if sentence_type == SentenceType.LAST:
                            enqueue_tts_report(self.conn, enqueue_text, enqueue_audio)
                            enqueue_audio, enqueue_text = [], None
                    else:
                        if enqueue_text is not None:
                            enqueue_tts_report(self.conn, enqueue_text, enqueue_audio)
                        enqueue_audio, enqueue_text = [], text
                if isinstance(audio_datas, bytes):
                    enqueue_audio.append(audio_datas)

                ctx = self._activity_ctx(sentence_id)
                if ctx and sentence_type == SentenceType.FIRST and not ctx["playback_started"]:
                    ctx["playback_started"] = True
                    now = time.time()
                    emit_turn(
                        "playback_started", ctx["turn_id"], source="tts",
                        session_id=getattr(self.conn, "session_id", None),
                        device_id=getattr(self.conn, "device_id", None),
                        tts_ms=max(0.0, (now - (ctx["tts_started"] or now)) * 1000.0),
                        first_audio_ms=max(0.0, (now - ctx["started"]) * 1000.0),
                    )

                future = asyncio.run_coroutine_threadsafe(
                    self._activity_send_audio(
                        sentence_type, audio_datas, text, sentence_id,
                    ),
                    self.conn.loop,
                )
                future.result()
                if self.conn.max_output_size > 0 and text:
                    add_device_output(self.conn.headers.get("device-id"), len(text))

                if ctx and sentence_type == SentenceType.LAST:
                    if not ctx.get("terminal"):
                        emit_turn(
                            "completed", ctx["turn_id"], source="tts",
                            session_id=getattr(self.conn, "session_id", None),
                            device_id=getattr(self.conn, "device_id", None),
                            total_ms=max(0.0, (time.time() - ctx["started"]) * 1000.0),
                        )
                    self._dotty_activity_sentences.pop(sentence_id, None)
            except Exception as exc:
                self.activity_tts_failed(sentence_id, exc)
