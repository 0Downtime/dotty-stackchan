"""Best-effort forwarding of perception events to the dashboard timeline."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

import requests

from perception import PerceptionEvent, PerceptionState


log = logging.getLogger("dotty-behaviour.consumers.activity_forwarder")

_FIRMWARE_EVENTS = {
    "face_detected", "face_lost", "sound_event", "head_pet_started",
    "head_pet_ended", "state_changed", "dance_started", "dance_ended",
    "wake_word_detected", "chat_status",
}


class ActivityForwarder:
    """Forward one private bus subscription without slowing other consumers."""

    def __init__(
        self,
        state: PerceptionState,
        url: str,
        *,
        token: str = "",
        timeout_sec: float = 0.75,
        post: Callable[..., Any] | None = None,
    ) -> None:
        self._state = state
        self._url = url.strip()
        self._token = token.strip()
        self._timeout_sec = max(0.05, timeout_sec)
        self._post = post or requests.post

    def _envelope(self, event: PerceptionEvent) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "event_id": uuid.uuid4().hex,
            "ts": event.ts if event.ts > 0 else time.time(),
            "source": "firmware" if event.name in _FIRMWARE_EVENTS else "behaviour",
            "kind": "event",
            "phase": "perception",
            "turn_id": None,
            "session_id": None,
            "device_id": event.device_id or None,
            "payload": {"name": event.name, "data": dict(event.data or {})},
        }

    def _send(self, event: PerceptionEvent) -> None:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Admin-Token"] = self._token
        response = self._post(
            self._url,
            json=self._envelope(event),
            headers=headers,
            timeout=self._timeout_sec,
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()

    async def forward(self, event: PerceptionEvent) -> bool:
        """Deliver one event on a worker thread; failures are deliberately soft."""
        try:
            await asyncio.to_thread(self._send, event)
            return True
        except Exception as exc:
            log.debug("activity delivery failed for %s: %s", event.name, exc)
            return False

    async def run(self) -> None:
        log.info("activity forwarder started (%s)", self._url)
        queue = self._state.subscribe()
        try:
            while True:
                await self.forward(await queue.get())
        finally:
            self._state.unsubscribe(queue)
