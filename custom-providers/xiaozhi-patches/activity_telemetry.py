"""Best-effort, non-blocking activity telemetry for xiaozhi-server.

Voice and playback threads only enqueue small sanitized envelopes. A single
daemon worker performs HTTP so an unavailable dashboard can never add latency
to a spoken turn.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.request
import uuid
from typing import Any, Callable


log = logging.getLogger("dotty.activity")
_QUEUE: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
_START_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_SENDER_OVERRIDE: Callable[[dict[str, Any]], None] | None = None


def enabled() -> bool:
    return os.environ.get("DOTTY_ACTIVITY_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def emit_turn(
    phase: str,
    turn_id: str | None,
    *,
    source: str = "xiaozhi",
    session_id: str | None = None,
    device_id: str | None = None,
    ts: float | None = None,
    **payload: Any,
) -> bool:
    if not enabled() or not turn_id:
        return False
    envelope = {
        "schema_version": 1,
        "event_id": uuid.uuid4().hex,
        "ts": ts or time.time(),
        "source": source,
        "kind": "turn",
        "phase": phase,
        "turn_id": str(turn_id)[:96],
        "session_id": str(session_id)[:128] if session_id else None,
        "device_id": str(device_id)[:128] if device_id else None,
        "payload": payload,
    }
    try:
        _QUEUE.put_nowait(envelope)
    except queue.Full:
        log.warning("activity queue full; dropping phase=%s", phase)
        return False
    _ensure_worker()
    return True


def _ensure_worker() -> None:
    global _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return
    with _START_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(
            target=_worker_loop, name="dotty-activity", daemon=True,
        )
        _WORKER.start()


def _worker_loop() -> None:
    while True:
        envelope = _QUEUE.get()
        try:
            if _SENDER_OVERRIDE is not None:
                _SENDER_OVERRIDE(envelope)
            else:
                _post(envelope)
        except Exception as exc:
            log.debug("activity delivery failed: %s", exc)
        finally:
            _QUEUE.task_done()


def _post(envelope: dict[str, Any]) -> None:
    url = os.environ.get(
        "DOTTY_ACTIVITY_URL", "http://127.0.0.1:8081/admin/activity",
    ).strip()
    if not url:
        return
    token = os.environ.get("DOTTY_ADMIN_TOKEN", "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Admin-Token"] = token
    request = urllib.request.Request(
        url,
        data=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=0.75) as response:
        response.read(1)


def _set_sender_for_tests(sender: Callable[[dict[str, Any]], None] | None) -> None:
    global _SENDER_OVERRIDE
    _SENDER_OVERRIDE = sender
