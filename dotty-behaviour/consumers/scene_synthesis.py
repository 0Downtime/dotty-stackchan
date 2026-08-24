"""Scene synthesis — periodic + event-driven composition of vision +
audio + state into a single sentence written to NDJSON.

Loop shape (per bridge.py's _scene_synthesis_loop):
  * single asyncio task races queue.get() vs an interval timeout
  * on a trigger event (face_recognized / audio_captioned /
    state_changed-into-trigger-state), compose for that device
  * on timeout, compose for every known device
  * MIN_GAP per-device suppresses bursts (multiple triggers in
    quick succession only emit once)
  * compose returns None if neither vision nor audio cache has
    anything fresh — face presence alone isn't enough signal

Each emit:
  1. writes the NDJSON record
  2. updates scene_synthesis_cache for the dashboard / prompt builder
  3. broadcasts a synthetic `scene_synthesised` event so the
     dashboard SSE feed refreshes immediately

No LLM call here — this is composition over caches that other paths
populated. Cheap, deterministic, runs even when narrative LLM is
offline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from time import perf_counter
from zoneinfo import ZoneInfo

from logs import NdjsonWriter
from perception import PerceptionState

log = logging.getLogger("dotty-behaviour.consumers.scene_synthesis")


def compose_scene_synthesis(
    state: PerceptionState,
    device_id: str,
    *,
    vision_ttl_sec: float,
    audio_ttl_sec: float,
    face_identity_ttl_sec: float,
    tz: ZoneInfo,
    now_perf: float | None = None,
) -> dict | None:
    """Compose one synthesis record from the current caches.

    Returns None when neither vision nor audio has a fresh entry.
    Face presence alone is too thin a signal — bridge.py's behaviour.
    """
    now_wall = time.time()
    now_p = now_perf if now_perf is not None else perf_counter()
    pstate = state.state.get(device_id) or {}

    vision_entry = state.vision_cache.get(device_id) or {}
    has_vision = bool(
        vision_entry
        and now_p - vision_entry.get("timestamp", 0.0) <= vision_ttl_sec
    )
    vision_desc = (
        (vision_entry.get("description") or "").strip()
        if has_vision else ""
    )

    audio_entry = state.audio_cache.get(device_id) or {}
    has_audio = bool(
        audio_entry
        and now_p - audio_entry.get("timestamp", 0.0) <= audio_ttl_sec
    )
    audio_desc = (
        (audio_entry.get("description") or "").strip()
        if has_audio else ""
    )

    if not has_vision and not has_audio:
        return None

    face_id = state.get_fresh_face_id(
        device_id, ttl_sec=face_identity_ttl_sec, now=now_wall
    )
    face_present = bool(pstate.get("face_present"))
    if face_id:
        face_phrase = f"{face_id} is in the room."
    elif face_present:
        face_phrase = "Someone is in the room."
    else:
        face_phrase = "No one is detected."

    current_state = pstate.get("current_state") or "idle"
    ts_label = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    parts: list[str] = [f"{ts_label} — {face_phrase}"]
    if vision_desc:
        parts.append(f"StackChan sees {vision_desc.rstrip('.')}.")
    if audio_desc:
        parts.append(f"Heard: {audio_desc.rstrip('.')}.")
    parts.append(f"State: {current_state}.")
    text = " ".join(parts)

    return {
        "ts": datetime.now(tz).isoformat(),
        "ts_wall": now_wall,
        "type": "scene_synthesis",
        "device": device_id,
        "text": text,
        "face_id": face_id,
        "state": current_state,
        "has_vision": has_vision,
        "has_audio_caption": has_audio,
    }


class SceneSynthesisLoop:
    def __init__(
        self,
        state: PerceptionState,
        writer: NdjsonWriter,
        *,
        interval_sec: float,
        min_gap_sec: float,
        trigger_events: frozenset[str],
        trigger_states: frozenset[str],
        vision_ttl_sec: float,
        audio_ttl_sec: float,
        face_identity_ttl_sec: float,
        tz: ZoneInfo,
    ) -> None:
        self._state = state
        self._writer = writer
        self._interval_sec = interval_sec
        self._min_gap_sec = min_gap_sec
        self._trigger_events = trigger_events
        self._trigger_states = trigger_states
        self._vision_ttl_sec = vision_ttl_sec
        self._audio_ttl_sec = audio_ttl_sec
        self._face_identity_ttl_sec = face_identity_ttl_sec
        self._tz = tz
        # Per-device last-emit wall time — used by the MIN_GAP guard.
        self._last_emit: dict[str, float] = {}

    def _maybe_emit(self, device_id: str, *, reason: str) -> None:
        record = compose_scene_synthesis(
            self._state,
            device_id,
            vision_ttl_sec=self._vision_ttl_sec,
            audio_ttl_sec=self._audio_ttl_sec,
            face_identity_ttl_sec=self._face_identity_ttl_sec,
            tz=self._tz,
        )
        if record is None:
            return
        now_wall = record["ts_wall"]
        last = self._last_emit.get(device_id, 0.0)
        if now_wall - last < self._min_gap_sec:
            return
        self._last_emit[device_id] = now_wall

        self._state.scene_synthesis_cache[device_id] = {
            "text": record["text"],
            "ts_wall": now_wall,
            "face_id": record["face_id"],
            "state": record["state"],
        }
        on_disk = {k: v for k, v in record.items() if k != "ts_wall"}
        self._writer.append(on_disk)
        log.info(
            "scene_synthesis device=%s reason=%s text=%s",
            device_id, reason, record["text"][:160],
        )

        from perception import PerceptionEvent

        try:
            self._state.broadcast(
                PerceptionEvent(
                    device_id=device_id,
                    name="scene_synthesised",
                    data={
                        "reason": reason,
                        "preview": record["text"][:80],
                    },
                    ts=now_wall,
                )
            )
        except Exception:
            log.warning("scene synthesis broadcast failed", exc_info=True)

    async def run(self) -> None:
        log.info(
            "scene synthesis loop started (interval=%.0fs min_gap=%.0fs)",
            self._interval_sec, self._min_gap_sec,
        )
        q = self._state.subscribe()
        try:
            while True:
                reason = "tick"
                try:
                    ev = await asyncio.wait_for(
                        q.get(), timeout=self._interval_sec
                    )
                except asyncio.TimeoutError:
                    ev = None

                if ev is not None:
                    if ev.name not in self._trigger_events:
                        continue
                    if ev.name == "state_changed":
                        new_state = (ev.data or {}).get("state")
                        if new_state not in self._trigger_states:
                            continue
                    reason = ev.name
                    device_ids = [ev.device_id] if ev.device_id else []
                else:
                    device_ids = list(self._state.state.keys())

                for did in device_ids:
                    if not did:
                        continue
                    try:
                        self._maybe_emit(did, reason=reason)
                    except Exception:
                        log.warning(
                            "scene synthesis emit failed device=%s",
                            did, exc_info=True,
                        )
        except asyncio.CancelledError:
            log.info("scene synthesis loop cancelled")
            raise
        except Exception:
            log.exception("scene synthesis loop crashed")
        finally:
            self._state.unsubscribe(q)
