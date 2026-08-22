"""Unified, bounded activity timeline for the Dotty dashboard."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


ActivitySource = Literal["xiaozhi", "pi", "tts", "firmware", "behaviour"]
ActivityKind = Literal["turn", "event"]
ActivityPhase = Literal[
    "asr_completed", "model_started", "tool_started", "tool_finished",
    "first_text", "response_ready", "tts_started", "playback_started",
    "completed", "failed", "aborted", "perception",
]

MAX_REQUEST_CHARS = 500
MAX_RESPONSE_CHARS = 1000
MAX_ERROR_CHARS = 300
MAX_TOOL_NAME_CHARS = 80
MAX_EVENT_NAME_CHARS = 80
MAX_EVENT_DATA_FIELDS = 16
MAX_EVENT_VALUE_CHARS = 200


def _safe_error(value: Any) -> str:
    """Bound errors to one line and redact common credential shapes."""
    text = str(value or "").splitlines()[0]
    text = re.sub(r"(?i)bearer\s+[a-z0-9._~+/=-]+", "Bearer <redacted>", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=<redacted>",
        text,
    )
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", text)
    return text[:MAX_ERROR_CHARS]


def _safe_event_data(value: Any) -> dict[str, Any]:
    """Keep compact scalar perception metadata and reject opaque payloads."""
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        if len(safe) >= MAX_EVENT_DATA_FIELDS:
            break
        key = str(raw_key)[:64]
        lowered = key.lower()
        if any(marker in lowered for marker in (
            "audio", "image", "frame", "bytes", "base64", "secret",
            "token", "password", "argument", "result", "payload",
        )):
            continue
        if isinstance(raw_value, bool) or raw_value is None:
            safe[key] = raw_value
        elif isinstance(raw_value, (int, float)):
            safe[key] = raw_value
        elif isinstance(raw_value, str):
            safe[key] = raw_value[:MAX_EVENT_VALUE_CHARS]
    return safe


class ActivityEnvelope(BaseModel):
    """Versioned, privacy-bounded wire contract for activity producers."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=96)
    ts: float = Field(gt=0)
    source: ActivitySource
    kind: ActivityKind
    phase: ActivityPhase
    turn_id: str | None = Field(default=None, max_length=96)
    session_id: str | None = Field(default=None, max_length=128)
    device_id: str | None = Field(default=None, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id")
    @classmethod
    def event_id_is_uuid(cls, value: str) -> str:
        try:
            UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("event_id must be a UUID") from exc
        return value

    @field_validator("turn_id")
    @classmethod
    def turn_events_need_id(cls, value: str | None, info):
        if info.data.get("kind") == "turn" and not value:
            raise ValueError("turn_id is required for turn events")
        if value:
            try:
                UUID(value)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("turn_id must be a UUID") from exc
        return value

    def safe_payload(self) -> dict[str, Any]:
        """Return only fields the dashboard contract permits."""
        p = self.payload
        if self.kind == "event":
            data = p.get("data")
            return {
                "name": str(p.get("name") or "event")[:MAX_EVENT_NAME_CHARS],
                "data": _safe_event_data(data),
            }
        safe: dict[str, Any] = {}
        if "request_text" in p:
            safe["request_text"] = str(p.get("request_text") or "")[:MAX_REQUEST_CHARS]
        if "response_text" in p:
            safe["response_text"] = str(p.get("response_text") or "")[:MAX_RESPONSE_CHARS]
        if "error" in p:
            safe["error"] = _safe_error(p.get("error"))
        if "emoji_used" in p:
            safe["emoji_used"] = str(p.get("emoji_used") or "")[:4]
        if "tool_name" in p:
            safe["tool_name"] = str(p.get("tool_name") or "")[:MAX_TOOL_NAME_CHARS]
        if "tool_call_id" in p:
            safe["tool_call_id"] = str(p.get("tool_call_id") or "")[:96]
        for key in (
            "asr_ms", "model_first_text_ms", "tool_duration_ms",
            "tts_ms", "first_audio_ms", "total_ms",
        ):
            value = p.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                safe[key] = round(float(value), 1)
        if "tool_ok" in p:
            safe["tool_ok"] = bool(p.get("tool_ok"))
        return safe


class ActivityStore:
    """Thread-safe fold/replay/broadcast store with bounded memory."""

    def __init__(self, max_items: int = 100, log_dir: Path | None = None) -> None:
        self.max_items = max(1, max_items)
        self.log_dir = log_dir or Path(
            os.environ.get("CONVO_LOG_DIR", "/var/lib/dotty-bridge/logs")
        )
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._seen_order: deque[str] = deque(maxlen=self.max_items * 5)
        self._seen: set[str] = set()
        self._listeners: list[asyncio.Queue] = []
        self._persisted_turns: set[str] = set()
        self._lock = threading.RLock()

    def _remember_event_id(self, event_id: str) -> bool:
        if event_id in self._seen:
            return False
        if len(self._seen_order) == self._seen_order.maxlen:
            oldest = self._seen_order.popleft()
            self._seen.discard(oldest)
        self._seen_order.append(event_id)
        self._seen.add(event_id)
        return True

    def ingest(self, envelope: ActivityEnvelope) -> dict[str, Any] | None:
        with self._lock:
            if not self._remember_event_id(envelope.event_id):
                return None
            payload = envelope.safe_payload()
            if envelope.kind == "event":
                item = {
                    "item_type": "event", "item_id": f"event:{envelope.event_id}",
                    "event_id": envelope.event_id, "ts": envelope.ts,
                    "updated_ts": envelope.ts, "source": envelope.source,
                    "phase": envelope.phase, "device_id": envelope.device_id,
                    **payload,
                }
            else:
                item = self._fold_turn(envelope, payload)
            self._items[item["item_id"]] = item
            self._items.move_to_end(item["item_id"])
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)
            if envelope.kind == "turn" and envelope.phase in {
                "completed", "failed", "aborted",
            }:
                self._persist_turn_once(item)
            snapshot = json.loads(json.dumps(item, ensure_ascii=False))
            for queue in list(self._listeners):
                try:
                    queue.put_nowait(snapshot)
                except asyncio.QueueFull:
                    pass
            return snapshot

    def _fold_turn(self, envelope: ActivityEnvelope, payload: dict[str, Any]) -> dict[str, Any]:
        assert envelope.turn_id is not None
        item_id = f"turn:{envelope.turn_id}"
        item = self._items.get(item_id)
        if item is None:
            item = {
                "item_type": "turn", "item_id": item_id,
                "turn_id": envelope.turn_id, "event_id": envelope.event_id,
                "ts": envelope.ts, "updated_ts": envelope.ts,
                "source": envelope.source, "phase": envelope.phase,
                "session_id": envelope.session_id, "device_id": envelope.device_id,
                "request_text": "", "response_text": "", "emoji_used": "",
                "error": "", "timings": {}, "tools": [],
            }
        previous_phase = item.get("phase")
        terminal_rank = {"completed": 1, "aborted": 2, "failed": 3}
        phase = envelope.phase
        if previous_phase in terminal_rank:
            if phase not in terminal_rank or terminal_rank[phase] < terminal_rank[previous_phase]:
                phase = previous_phase
        item.update({"event_id": envelope.event_id, "updated_ts": envelope.ts,
                     "source": envelope.source, "phase": phase})
        if envelope.session_id:
            item["session_id"] = envelope.session_id
        if envelope.device_id:
            item["device_id"] = envelope.device_id
        for name in ("request_text", "response_text", "emoji_used", "error"):
            if name in payload:
                item[name] = payload[name]
        for timing in ("asr_ms", "model_first_text_ms", "tts_ms", "first_audio_ms", "total_ms"):
            if timing in payload:
                item["timings"][timing] = payload[timing]
        if envelope.phase == "tool_started":
            item["tools"].append({
                "id": payload.get("tool_call_id") or envelope.event_id,
                "name": payload.get("tool_name") or "tool",
                "status": "running", "duration_ms": None,
            })
        elif envelope.phase == "tool_finished":
            tool_id = payload.get("tool_call_id")
            match = next((tool for tool in reversed(item["tools"])
                          if (tool_id and tool["id"] == tool_id)
                          or (not tool_id and tool["name"] == payload.get("tool_name"))), None)
            if match is None:
                match = {"id": tool_id or envelope.event_id,
                         "name": payload.get("tool_name") or "tool"}
                item["tools"].append(match)
            match["status"] = "ok" if payload.get("tool_ok", True) else "error"
            match["duration_ms"] = payload.get("tool_duration_ms")
        return item

    def replay(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(list(self._items.values()), ensure_ascii=False))

    def subscribe(self) -> tuple[asyncio.Queue, list[dict[str, Any]]]:
        with self._lock:
            queue: asyncio.Queue = asyncio.Queue(maxsize=self.max_items)
            self._listeners.append(queue)
            return queue, self.replay()

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            try:
                self._listeners.remove(queue)
            except ValueError:
                pass

    def listener_count(self) -> int:
        with self._lock:
            return len(self._listeners)

    def _today_path(self) -> Path:
        return self.log_dir / f"convo-{datetime.now().strftime('%Y-%m-%d')}.ndjson"

    def _persist_turn_once(self, item: dict[str, Any]) -> None:
        turn_id = item["turn_id"]
        if turn_id in self._persisted_turns:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": datetime.fromtimestamp(item.get("updated_ts") or item.get("ts"), timezone.utc).isoformat(),
                "turn_id": turn_id, "channel": "stackchan",
                "request_text": item.get("request_text", "")[:MAX_REQUEST_CHARS],
                "response_text": item.get("response_text", "")[:MAX_RESPONSE_CHARS],
                "latency_ms": item.get("timings", {}).get("total_ms"),
                "error": item.get("error") or None,
                "emoji_used": item.get("emoji_used") or None,
                "phase": item.get("phase"),
            }
            with self._today_path().open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._persisted_turns.add(turn_id)
        except OSError:
            pass


activity_store = ActivityStore()
