"""ActivityForwarder remains best-effort across bridge failures."""

from __future__ import annotations

import asyncio
import time
import unittest

import requests

from consumers.activity_forwarder import ActivityForwarder
from perception import PerceptionEvent, PerceptionState


class _Response:
    def raise_for_status(self) -> None:
        return None


class ActivityForwarderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = PerceptionState()
        self.event = PerceptionEvent(
            device_id="device-1", name="face_detected",
            data={"confidence": 0.9}, ts=123.0,
        )

    async def test_success_uses_token_and_private_envelope(self):
        calls = []

        def post(*args, **kwargs):
            calls.append((args, kwargs))
            return _Response()

        forwarder = ActivityForwarder(
            self.state, "http://127.0.0.1:8081/admin/activity",
            token="shared", post=post,
        )
        self.assertTrue(await forwarder.forward(self.event))
        _, kwargs = calls[0]
        self.assertEqual(kwargs["headers"]["X-Admin-Token"], "shared")
        self.assertEqual(kwargs["json"]["kind"], "event")
        self.assertEqual(kwargs["json"]["source"], "firmware")
        self.assertEqual(kwargs["json"]["phase"], "perception")
        self.assertEqual(kwargs["json"]["payload"]["name"], "face_detected")

    def test_synthetic_event_is_sourced_from_behaviour(self):
        forwarder = ActivityForwarder(self.state, "http://bridge")
        event = PerceptionEvent(
            device_id="device-1", name="head_turn", data={}, ts=123.0,
        )
        self.assertEqual(forwarder._envelope(event)["source"], "behaviour")

    async def test_timeout_and_outage_are_soft_failures(self):
        for exc in (
            requests.Timeout("slow"),
            requests.ConnectionError("offline"),
        ):
            def post(*args, _exc=exc, **kwargs):
                raise _exc

            forwarder = ActivityForwarder(self.state, "http://bridge", post=post)
            self.assertFalse(await forwarder.forward(self.event))

    async def test_slow_forward_does_not_block_other_bus_consumers(self):
        def post(*args, **kwargs):
            time.sleep(0.1)
            return _Response()

        other = self.state.subscribe()
        forwarder = ActivityForwarder(self.state, "http://bridge", post=post)
        task = asyncio.create_task(forwarder.run())
        await asyncio.sleep(0)
        self.state.broadcast(self.event)
        received = await asyncio.wait_for(other.get(), timeout=0.02)
        self.assertEqual(received.name, "face_detected")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.state.unsubscribe(other)


if __name__ == "__main__":
    unittest.main()
