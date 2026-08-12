"""OpenAI Realtime speech-to-speech bridge for Dotty.

The StackChan firmware keeps speaking the Xiaozhi protocol (16 kHz Opus up,
24 kHz Opus down).  This module wraps one Xiaozhi ``ConnectionHandler`` and,
when explicitly enabled, translates a voice turn to an OpenAI Realtime
WebSocket session.  The ordinary VAD -> ASR -> PiVoiceLLM -> Piper route is
left intact and receives every message the bridge declines.

Safety boundary: Realtime is never used while Kid Mode is active.  Dotty's
Kid Mode filter makes an atomic decision over the complete text before local
TTS starts; an audio-native stream cannot preserve that guarantee.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote


_INPUT_RATE = 16_000
_OUTPUT_RATE = 24_000
_FRAME_MS = 60
_INPUT_FRAME_SAMPLES = _INPUT_RATE * _FRAME_MS // 1000
_OUTPUT_FRAME_SAMPLES = _OUTPUT_RATE * _FRAME_MS // 1000
_OUTPUT_FRAME_BYTES = _OUTPUT_FRAME_SAMPLES * 2
_QUEUE_LIMIT = 200  # 12 seconds of 60 ms frames; bounds memory on a slow device.


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def kid_mode_active() -> bool:
    """Read the shared runtime toggle, falling back to startup config."""
    state_file = Path(
        os.environ.get(
            "DOTTY_KID_MODE_STATE",
            "/var/lib/dotty-bridge/state/kid-mode",
        )
    )
    try:
        value = state_file.read_text().strip().lower()
        if value in {"true", "1", "yes", "on"}:
            return True
        if value in {"false", "0", "no", "off"}:
            return False
    except OSError:
        pass
    return _env_bool("DOTTY_KID_MODE", True)


@dataclass(frozen=True)
class RealtimeSettings:
    enabled: bool = False
    api_key: str = field(default="", repr=False)
    model: str = "gpt-realtime-2.1-mini"
    voice: str = "marin"
    transcription_model: str = "gpt-live-transcribe"
    reasoning_effort: str = "low"
    base_url: str = "wss://api.openai.com/v1/realtime"
    connect_timeout_seconds: float = 10.0
    event_timeout_seconds: float = 8.0

    @classmethod
    def from_env(cls) -> "RealtimeSettings":
        return cls(
            enabled=_env_bool("DOTTY_REALTIME_ENABLED", False),
            api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
            model=os.environ.get(
                "DOTTY_REALTIME_MODEL", "gpt-realtime-2.1-mini"
            ).strip(),
            voice=os.environ.get("DOTTY_REALTIME_VOICE", "marin").strip(),
            transcription_model=os.environ.get(
                "DOTTY_REALTIME_TRANSCRIPTION_MODEL", "gpt-live-transcribe"
            ).strip(),
            reasoning_effort=os.environ.get(
                "DOTTY_REALTIME_REASONING_EFFORT", "low"
            ).strip(),
            base_url=os.environ.get(
                "DOTTY_REALTIME_URL", "wss://api.openai.com/v1/realtime"
            ).rstrip("/"),
            connect_timeout_seconds=_env_float(
                "DOTTY_REALTIME_CONNECT_TIMEOUT_SECONDS", 10.0, 1.0
            ),
            event_timeout_seconds=_env_float(
                "DOTTY_REALTIME_EVENT_TIMEOUT_SECONDS", 8.0, 1.0
            ),
        )

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key and self.model)

    @property
    def websocket_url(self) -> str:
        separator = "&" if "?" in self.base_url else "?"
        return f"{self.base_url}{separator}model={quote(self.model, safe='')}"


class OpusPcmCodec:
    """Stateful StackChan Opus <-> OpenAI PCM24 translator.

    Imports are delayed so config-only startup and unit tests do not require
    codec libraries.  The pinned Xiaozhi image already supplies opuslib-next,
    NumPy, and SciPy.
    """

    def __init__(self) -> None:
        import numpy as np
        import opuslib_next
        from scipy.signal import resample_poly

        self._np = np
        self._resample_poly = resample_poly
        self._decoder = opuslib_next.Decoder(_INPUT_RATE, 1)
        self._encoder = opuslib_next.Encoder(
            _OUTPUT_RATE, 1, opuslib_next.APPLICATION_AUDIO
        )
        self._output_buffer = bytearray()

    def decode_input_opus(self, packet: bytes) -> bytes:
        pcm16 = self._decoder.decode(packet, _INPUT_FRAME_SAMPLES)
        samples = self._np.frombuffer(pcm16, dtype=self._np.int16)
        if samples.size == 0:
            return b""
        resampled = self._resample_poly(samples.astype(self._np.float32), 3, 2)
        return self._np.clip(resampled, -32768, 32767).astype(self._np.int16).tobytes()

    def encode_output_pcm(self, pcm24: bytes) -> list[bytes]:
        self._output_buffer.extend(pcm24)
        packets: list[bytes] = []
        while len(self._output_buffer) >= _OUTPUT_FRAME_BYTES:
            frame = bytes(self._output_buffer[:_OUTPUT_FRAME_BYTES])
            del self._output_buffer[:_OUTPUT_FRAME_BYTES]
            packets.append(self._encoder.encode(frame, _OUTPUT_FRAME_SAMPLES))
        return packets

    def flush_output(self) -> list[bytes]:
        if not self._output_buffer:
            return []
        frame = bytes(self._output_buffer)
        self._output_buffer.clear()
        frame += b"\x00" * (_OUTPUT_FRAME_BYTES - len(frame))
        return [self._encoder.encode(frame, _OUTPUT_FRAME_SAMPLES)]

    def reset_output(self) -> None:
        self._output_buffer.clear()


ConnectFactory = Callable[[str, dict[str, str]], Awaitable[Any]]
CodecFactory = Callable[[], Any]


async def _default_connect(url: str, headers: dict[str, str]):
    import websockets

    return await websockets.connect(url, additional_headers=headers)


class OpenAIRealtimeBridge:
    """Own one OpenAI Realtime session for one Xiaozhi connection."""

    def __init__(
        self,
        conn: Any,
        settings: RealtimeSettings | None = None,
        *,
        connect_factory: ConnectFactory | None = None,
        codec_factory: CodecFactory | None = None,
    ) -> None:
        self.conn = conn
        self.settings = settings or RealtimeSettings.from_env()
        self._connect_factory = connect_factory or _default_connect
        self._codec_factory = codec_factory or OpusPcmCodec
        self._ws: Any | None = None
        self._codec: Any | None = None
        self._receiver_task: asyncio.Task | None = None
        self._player_task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self._created_event = asyncio.Event()
        self._updated_event = asyncio.Event()
        self._output_queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._closed = False
        self._active_input = False
        self._input_packets = 0
        self._response_active = False
        self._response_id: str | None = None
        self._output_item_id: str | None = None
        self._played_ms = 0
        self._playback_epoch = 0
        self._audio_done_epoch: int | None = None
        self._tts_started = False
        self._transcript = ""
        self._warned_missing_key = False
        self._session_listen_mode: str | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._receiver_task is not None

    @property
    def active_input(self) -> bool:
        return self._active_input

    def _log(self, level: str, message: str) -> None:
        logger = getattr(self.conn, "logger", None)
        try:
            bound = logger.bind(tag=__name__)
            getattr(bound, level)(message)
        except (AttributeError, TypeError):
            import logging

            getattr(logging.getLogger(__name__), level)(message)

    def _headers(self) -> dict[str, str]:
        device_id = str(
            getattr(self.conn, "device_id", "")
            or (getattr(self.conn, "headers", {}) or {}).get("device-id", "")
            or "anonymous-dotty-device"
        )
        safety_id = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
        return {
            "Authorization": f"Bearer {self.settings.api_key}",
            "OpenAI-Safety-Identifier": safety_id,
            "User-Agent": "dotty-stackchan-realtime/0.1",
        }

    def _instructions(self) -> str:
        base = str((getattr(self.conn, "config", {}) or {}).get("prompt", "")).strip()
        realtime_rules = (
            "You are speaking directly through Dotty, a small desktop robot. "
            "Ignore any earlier instruction requiring an emoji prefix: do not "
            "speak or output an emoji. "
            "Reply in natural spoken English without markdown, lists, emoji names, "
            "or stage directions. Keep ordinary replies to one or two short sentences. "
            "Do not claim a local action, memory lookup, camera result, device status, "
            "or song succeeded unless you used the consult_dotty_local_agent tool."
        )
        return f"{base}\n\n{realtime_rules}" if base else realtime_rules

    def _tool_definitions(self) -> list[dict[str, Any]]:
        llm = getattr(self.conn, "llm", None)
        if not callable(getattr(llm, "response", None)):
            return []
        return [
            {
                "type": "function",
                "name": "consult_dotty_local_agent",
                "description": (
                    "Use Dotty's private local agent for remembered facts, current "
                    "device status or control, camera/photo requests, songs, or deeper "
                    "reasoning. Do not use it for greetings or ordinary conversation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request": {
                            "type": "string",
                            "description": "The user's request, with relevant context.",
                        }
                    },
                    "required": ["request"],
                    "additionalProperties": False,
                },
            }
        ]

    def _listen_mode(self) -> str:
        return str(getattr(self.conn, "client_listen_mode", "") or "").strip().lower()

    def _uses_server_vad(self) -> bool:
        return self._listen_mode() in {"auto", "realtime"}

    def _session_update(self) -> dict[str, Any]:
        turn_detection = None
        if self._uses_server_vad():
            turn_detection = {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
                "create_response": True,
                "interrupt_response": True,
            }
        input_audio: dict[str, Any] = {
            "format": {"type": "audio/pcm", "rate": _OUTPUT_RATE},
            "turn_detection": turn_detection,
        }
        if self.settings.transcription_model:
            input_audio["transcription"] = {
                "model": self.settings.transcription_model
            }
        session: dict[str, Any] = {
            "type": "realtime",
            "model": self.settings.model,
            "output_modalities": ["audio"],
            "instructions": self._instructions(),
            "audio": {
                "input": input_audio,
                "output": {
                    "format": {"type": "audio/pcm", "rate": _OUTPUT_RATE},
                    "voice": self.settings.voice,
                },
            },
            "reasoning": {"effort": self.settings.reasoning_effort},
            "tools": self._tool_definitions(),
            "tool_choice": "auto",
        }
        return {"type": "session.update", "session": session}

    async def ensure_connected(self) -> bool:
        if self._closed or not self.settings.enabled or kid_mode_active():
            return False
        if not self.settings.configured:
            if not self._warned_missing_key:
                self._log(
                    "warning",
                    "OpenAI Realtime enabled without OPENAI_API_KEY; using local voice path",
                )
                self._warned_missing_key = True
            return False
        desired_listen_mode = self._listen_mode()
        if (
            self.connected
            and not self._receiver_task.done()
            and self._session_listen_mode == desired_listen_mode
        ):
            return True

        async with self._connect_lock:
            if (
                self.connected
                and not self._receiver_task.done()
                and self._session_listen_mode == desired_listen_mode
            ):
                return True
            await self._disconnect(send_device_stop=False)
            self._closed = False
            self._created_event = asyncio.Event()
            self._updated_event = asyncio.Event()
            try:
                self._ws = await asyncio.wait_for(
                    self._connect_factory(self.settings.websocket_url, self._headers()),
                    timeout=self.settings.connect_timeout_seconds,
                )
                self._codec = self._codec_factory()
                self._receiver_task = asyncio.create_task(
                    self._receive_events(), name="dotty-openai-realtime-receiver"
                )
                self._player_task = asyncio.create_task(
                    self._play_output(), name="dotty-openai-realtime-player"
                )
                await asyncio.wait_for(
                    self._created_event.wait(),
                    timeout=self.settings.event_timeout_seconds,
                )
                await self._send_openai(self._session_update())
                await asyncio.wait_for(
                    self._updated_event.wait(),
                    timeout=self.settings.event_timeout_seconds,
                )
                self._session_listen_mode = desired_listen_mode
            except Exception as exc:
                self._log(
                    "warning",
                    f"OpenAI Realtime connection unavailable ({type(exc).__name__}); using local voice path",
                )
                await self._disconnect(send_device_stop=False)
                return False
            self._log(
                "info",
                f"OpenAI Realtime ready model={self.settings.model} voice={self.settings.voice}",
            )
            return True

    async def _send_openai(self, event: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionError("OpenAI Realtime session is not connected")
        await self._ws.send(json.dumps(event, separators=(",", ":")))

    async def begin_input(self) -> bool:
        if not await self.ensure_connected():
            return False
        if self._response_active or self._tts_started:
            await self.interrupt()
        self._active_input = True
        self._input_packets = 0
        self.conn.client_abort = False
        if callable(getattr(self.conn, "reset_audio_states", None)):
            self.conn.reset_audio_states()
        try:
            await self._send_openai({"type": "input_audio_buffer.clear"})
        except Exception as exc:
            self._active_input = False
            self._log(
                "warning",
                f"OpenAI Realtime turn start failed ({type(exc).__name__}); using local voice path",
            )
            await self._disconnect(send_device_stop=False)
            return False
        return True

    async def push_opus(self, packet: bytes) -> bool:
        if not self._active_input or self._ws is None or self._codec is None:
            return False
        # Keep this route half-duplex while Dotty is speaking. The device's
        # acoustic echo cancellation is not strong enough to guarantee that
        # its own speaker audio will not retrigger server VAD, which can cause
        # a response loop. Consume (drop) mic frames until playback finishes.
        if self._response_active or self._tts_started:
            return True
        try:
            pcm24 = self._codec.decode_input_opus(packet)
            if pcm24:
                await self._send_openai(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm24).decode("ascii"),
                    }
                )
                self._input_packets += 1
            return True
        except Exception as exc:
            self._log(
                "warning",
                f"OpenAI Realtime input failed ({type(exc).__name__}); falling back next turn",
            )
            self._active_input = False
            await self._disconnect(send_device_stop=True)
            return False

    async def end_input(self) -> bool:
        if not self._active_input:
            return False
        self._active_input = False
        if self._uses_server_vad():
            return True
        if self._input_packets == 0:
            return True
        try:
            await self._send_openai({"type": "input_audio_buffer.commit"})
            await self._send_openai({"type": "response.create"})
        except Exception as exc:
            self._log(
                "warning",
                f"OpenAI Realtime turn commit failed ({type(exc).__name__}); falling back next turn",
            )
            await self._disconnect(send_device_stop=True)
        return True

    async def send_text(self, text: str) -> bool:
        if not text.strip() or not await self.ensure_connected():
            return False
        try:
            await self._send_openai(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text.strip()}],
                    },
                }
            )
            await self._send_openai({"type": "response.create"})
        except Exception as exc:
            self._log(
                "warning",
                f"OpenAI Realtime text turn failed ({type(exc).__name__}); using local voice path",
            )
            await self._disconnect(send_device_stop=False)
            return False
        return True

    async def interrupt(self) -> None:
        old_item = self._output_item_id
        played_ms = self._played_ms
        self._playback_epoch += 1
        self._response_active = False
        self._response_id = None
        self._output_item_id = None
        self._played_ms = 0
        self._audio_done_epoch = None
        self._transcript = ""
        if self._codec is not None:
            self._codec.reset_output()
        self._drain_output_queue()
        if self._ws is not None:
            try:
                await self._send_openai({"type": "response.cancel"})
            except Exception:
                pass
            if old_item and played_ms > 0:
                try:
                    await self._send_openai(
                        {
                            "type": "conversation.item.truncate",
                            "item_id": old_item,
                            "content_index": 0,
                            "audio_end_ms": played_ms,
                        }
                    )
                except Exception:
                    pass
        await self._stop_device_audio()

    async def handle_device_message(self, message: str | bytes) -> bool:
        """Return True when the Realtime route consumed the device message."""
        if not self.settings.enabled:
            return False
        if kid_mode_active():
            if self.connected:
                await self._disconnect(send_device_stop=True)
            return False
        if isinstance(message, bytes):
            return await self.push_opus(message)
        try:
            payload = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False

        message_type = payload.get("type")
        if message_type == "listen":
            state = payload.get("state")
            if "mode" in payload:
                self.conn.client_listen_mode = payload["mode"]
            if state == "start":
                return await self.begin_input()
            if state == "stop":
                return await self.end_input()
            if state == "detect" and isinstance(payload.get("text"), str):
                return await self.send_text(payload["text"])
        if message_type == "abort" and self.connected:
            await self.interrupt()
            return False  # let Xiaozhi perform its normal queue/state cleanup too
        return False

    async def _receive_events(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    event = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict):
                    await self._handle_server_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                self._log(
                    "warning",
                    f"OpenAI Realtime receiver stopped ({type(exc).__name__}); next turn uses local fallback",
                )
        finally:
            self._active_input = False

    async def _handle_server_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "")
        if event_type == "session.created":
            self._created_event.set()
            return
        if event_type == "session.updated":
            self._updated_event.set()
            return
        if event_type == "conversation.item.input_audio_transcription.completed":
            transcript = str(event.get("transcript", "")).strip()
            if transcript:
                await self._send_device_json({"type": "stt", "text": transcript})
                await self._send_device_json({"type": "tts", "state": "start"})
            return
        if event_type == "response.created":
            response = event.get("response") or {}
            self._response_active = True
            self._response_id = response.get("id")
            self._transcript = ""
            self._audio_done_epoch = None
            return
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "message":
                self._output_item_id = item.get("id")
            return
        if event_type in {"response.output_audio_transcript.delta", "response.audio_transcript.delta"}:
            self._transcript += str(event.get("delta", ""))
            return
        if event_type in {"response.output_audio.delta", "response.audio.delta"}:
            if self._codec is None:
                return
            try:
                pcm = base64.b64decode(event.get("delta", ""), validate=True)
            except (TypeError, ValueError):
                return
            for packet in self._codec.encode_output_pcm(pcm):
                await self._output_queue.put((self._playback_epoch, packet))
            return
        if event_type in {"response.output_audio.done", "response.audio.done"}:
            await self._queue_audio_done()
            return
        if event_type == "response.done":
            self._response_active = False
            response = event.get("response") or {}
            tool_calls = [
                item
                for item in response.get("output", [])
                if isinstance(item, dict) and item.get("type") == "function_call"
            ]
            if tool_calls:
                await self._complete_tool_calls(tool_calls)
            elif self._tts_started and self._audio_done_epoch != self._playback_epoch:
                await self._queue_audio_done()
            return
        if event_type == "error":
            error = event.get("error") or event
            code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
            self._log("error", f"OpenAI Realtime error code={code}")

    async def _queue_audio_done(self) -> None:
        if self._audio_done_epoch == self._playback_epoch:
            return
        if self._codec is not None:
            for packet in self._codec.flush_output():
                await self._output_queue.put((self._playback_epoch, packet))
        self._audio_done_epoch = self._playback_epoch
        await self._output_queue.put((self._playback_epoch, None))

    async def _play_output(self) -> None:
        while True:
            epoch, packet = await self._output_queue.get()
            try:
                if epoch != self._playback_epoch:
                    continue
                # The dashboard can enable Kid Mode while a response is
                # already streaming. Re-check before every 60 ms frame so the
                # audio-native path cannot continue past that safety boundary.
                if kid_mode_active():
                    await self.interrupt()
                    self._active_input = False
                    continue
                if packet is None:
                    await self._stop_device_audio()
                    self._transcript = ""
                    self._audio_done_epoch = None
                    continue
                if not self._tts_started:
                    await self._send_device_json(
                        {"type": "llm", "text": "😐", "emotion": "neutral"}
                    )
                    await self._send_device_json(
                        {
                            "type": "tts",
                            "state": "sentence_start",
                            "text": self._transcript.strip(),
                        }
                    )
                    self._tts_started = True
                    self.conn.client_is_speaking = True
                await self._send_device(packet)
                self._played_ms += _FRAME_MS
                await asyncio.sleep(_FRAME_MS / 1000.0)
            finally:
                self._output_queue.task_done()

    async def _complete_tool_calls(self, calls: list[dict[str, Any]]) -> None:
        for call in calls:
            call_id = call.get("call_id")
            if not call_id:
                continue
            output = await self._execute_tool(call)
            await self._send_openai(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    },
                }
            )
        await self._send_openai({"type": "response.create"})

    async def _execute_tool(self, call: dict[str, Any]) -> str:
        if call.get("name") != "consult_dotty_local_agent":
            return json.dumps({"error": "unknown tool"})
        try:
            arguments = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError:
            return json.dumps({"error": "invalid arguments"})
        request = str(arguments.get("request", "")).strip()
        llm = getattr(self.conn, "llm", None)
        if not request or not callable(getattr(llm, "response", None)):
            return json.dumps({"error": "local agent unavailable"})

        def run_local_agent() -> str:
            dialogue = [{"role": "user", "content": request}]
            chunks = llm.response(getattr(self.conn, "session_id", ""), dialogue)
            return "".join(str(chunk) for chunk in chunks).strip()

        try:
            result = await asyncio.to_thread(run_local_agent)
        except Exception as exc:
            self._log("error", f"Realtime local-agent tool failed ({type(exc).__name__})")
            return json.dumps({"error": "local agent failed"})
        return json.dumps({"result": result[:4000] or "No result"}, ensure_ascii=False)

    async def _send_device_json(self, payload: dict[str, Any]) -> None:
        payload.setdefault("session_id", getattr(self.conn, "session_id", ""))
        await self._send_device(json.dumps(payload, ensure_ascii=False))

    async def _send_device(self, message: str | bytes) -> None:
        try:
            from core.utils.device_command import send_serialized
        except ImportError:
            await self.conn.websocket.send(message)
        else:
            await send_serialized(self.conn, message)

    async def _stop_device_audio(self) -> None:
        if self._tts_started or getattr(self.conn, "client_is_speaking", False):
            try:
                await self._send_device_json({"type": "tts", "state": "stop"})
            except Exception:
                pass
        self._tts_started = False
        self.conn.client_is_speaking = False
        clear_status = getattr(self.conn, "clearSpeakStatus", None)
        if callable(clear_status):
            clear_status()

    def _drain_output_queue(self) -> None:
        while True:
            try:
                self._output_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            else:
                self._output_queue.task_done()

    async def _disconnect(self, *, send_device_stop: bool) -> None:
        if send_device_stop:
            await self._stop_device_audio()
        current = asyncio.current_task()
        for task in (self._receiver_task, self._player_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        for task in (self._receiver_task, self._player_task):
            if task is not None and task is not current:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._receiver_task = None
        self._player_task = None
        self._codec = None
        self._session_listen_mode = None
        self._active_input = False
        self._response_active = False
        self._drain_output_queue()

    async def close(self) -> None:
        self._closed = True
        await self._disconnect(send_device_stop=True)


def attach_realtime_bridge(
    conn: Any,
    settings: RealtimeSettings | None = None,
    **bridge_kwargs: Any,
) -> OpenAIRealtimeBridge | None:
    """Wrap ``conn._route_message`` when Realtime was explicitly enabled."""
    selected = settings or RealtimeSettings.from_env()
    if not selected.enabled:
        return None
    bridge = OpenAIRealtimeBridge(conn, selected, **bridge_kwargs)
    original_route = conn._route_message

    async def route_with_realtime(message: str | bytes):
        # Preserve Xiaozhi's bind/auth gate.  Before it resolves, the original
        # route owns the message and may emit a bind prompt.
        bind_event = getattr(conn, "bind_completed_event", None)
        if bind_event is not None and not bind_event.is_set():
            return await original_route(message)
        if getattr(conn, "need_bind", False):
            return await original_route(message)
        if await bridge.handle_device_message(message):
            return None
        return await original_route(message)

    conn._route_message = route_with_realtime
    return bridge
