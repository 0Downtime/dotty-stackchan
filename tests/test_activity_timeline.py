"""Contract tests for the unified bridge activity timeline."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from pydantic import ValidationError

from bridge.activity import ActivityEnvelope, ActivityStore
from bridge import dashboard as dashboard_module


TURN_ID = "11111111-1111-4111-8111-111111111111"


def envelope(
    event_id: str,
    *,
    kind: str = "turn",
    phase: str = "asr_completed",
    turn_id: str | None = TURN_ID,
    ts: float = 100.0,
    payload: dict | None = None,
) -> ActivityEnvelope:
    return ActivityEnvelope(
        schema_version=1,
        event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, event_id)),
        ts=ts,
        source="xiaozhi" if kind == "turn" else "firmware",
        kind=kind,
        phase=phase,
        turn_id=turn_id if kind == "turn" else None,
        session_id="session-1" if kind == "turn" else None,
        device_id="device-1",
        payload=payload or {},
    )


class ActivityEnvelopeTests(unittest.TestCase):
    def test_turn_requires_turn_id_and_unknown_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            envelope("bad", turn_id=None)
        with self.assertRaises(ValidationError):
            ActivityEnvelope(**{
                **envelope("ok").model_dump(), "unexpected": True,
            })

    def test_privacy_bounds_and_payload_allowlist(self):
        safe = envelope("privacy", payload={
            "request_text": "r" * 700,
            "response_text": "s" * 1300,
            "error": "token=super-secret failed\ntraceback should be dropped",
            "tool_name": "lookup",
            "tool_arguments": {"secret": "never"},
            "tool_result": "never",
            "raw_audio": b"never",
        }).safe_payload()
        self.assertEqual(len(safe["request_text"]), 500)
        self.assertEqual(len(safe["response_text"]), 1000)
        self.assertEqual(safe["error"], "token=<redacted> failed")
        self.assertNotIn("tool_arguments", safe)
        self.assertNotIn("tool_result", safe)
        self.assertNotIn("raw_audio", safe)

    def test_event_data_drops_media_secrets_and_nested_payloads(self):
        safe = envelope(
            "event-privacy", kind="event", phase="perception",
            payload={"name": "sound_event", "data": {
                "direction": "left", "energy": 0.75,
                "audio_base64": "private", "token": "private",
                "nested": {"not": "allowed"},
            }},
        ).safe_payload()
        self.assertEqual(safe["data"], {"direction": "left", "energy": 0.75})


class ActivityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = ActivityStore(max_items=3, log_dir=Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_turn_stages_fold_in_place_and_tool_details_are_sanitized(self):
        self.store.ingest(envelope("one", payload={"request_text": "weather"}))
        self.store.ingest(envelope(
            "two", phase="tool_started", ts=101,
            payload={"tool_call_id": "tc-1", "tool_name": "weather",
                     "arguments": {"location": "private"}},
        ))
        item = self.store.ingest(envelope(
            "three", phase="tool_finished", ts=102,
            payload={"tool_call_id": "tc-1", "tool_name": "weather",
                     "tool_ok": True, "tool_duration_ms": 14.2,
                     "result": "private"},
        ))
        self.assertEqual(len(self.store.replay()), 1)
        self.assertEqual(item["tools"], [{
            "id": "tc-1", "name": "weather", "status": "ok",
            "duration_ms": 14.2,
        }])
        self.assertNotIn("private", json.dumps(item))

    def test_event_id_dedup_and_bounded_replay(self):
        first = envelope("same", kind="event", phase="perception", ts=100)
        self.assertIsNotNone(self.store.ingest(first))
        self.assertIsNone(self.store.ingest(first))
        for index in range(1, 5):
            self.store.ingest(envelope(
                f"ev-{index}", kind="event", phase="perception",
                ts=100 + index, payload={"name": f"event-{index}"},
            ))
        replay = self.store.replay()
        self.assertEqual(len(replay), 3)
        self.assertEqual([item["name"] for item in replay], ["event-2", "event-3", "event-4"])

    def test_subscribe_replay_is_atomic_and_slow_consumer_drops(self):
        self.store.ingest(envelope("before"))
        queue, replay = self.store.subscribe()
        self.assertEqual([item["item_id"] for item in replay], [f"turn:{TURN_ID}"])
        for index in range(10):
            self.store.ingest(envelope(
                f"live-{index}", kind="event", phase="perception",
                ts=200 + index,
            ))
        self.assertEqual(queue.qsize(), 3)
        self.store.unsubscribe(queue)
        self.assertEqual(self.store.listener_count(), 0)

    def test_terminal_turn_is_persisted_once(self):
        self.store.ingest(envelope("start", payload={"request_text": "hello"}))
        self.store.ingest(envelope(
            "done", phase="completed", ts=103,
            payload={"response_text": "hi", "total_ms": 300},
        ))
        self.store.ingest(envelope(
            "late-duplicate-terminal", phase="completed", ts=104,
            payload={"response_text": "hi again"},
        ))
        paths = list(Path(self.temp.name).glob("convo-*.ndjson"))
        self.assertEqual(len(paths), 1)
        records = [json.loads(line) for line in paths[0].read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["turn_id"], TURN_ID)
        self.assertEqual(records[0]["phase"], "completed")

    def test_failure_is_not_overwritten_by_fallback_playback_updates(self):
        self.store.ingest(envelope("start"))
        self.store.ingest(envelope(
            "brain-failed", phase="failed", ts=101,
            payload={"error": "brain offline", "response_text": "fallback"},
        ))
        self.store.ingest(envelope("fallback-speaking", phase="playback_started", ts=102))
        final = self.store.ingest(envelope("fallback-done", phase="completed", ts=103))
        self.assertEqual(final["phase"], "failed")
        self.assertEqual(final["error"], "brain offline")
        log_path = next(Path(self.temp.name).glob("convo-*.ndjson"))
        persisted = json.loads(log_path.read_text().splitlines()[0])
        self.assertEqual(persisted["phase"], "failed")
        self.assertEqual(persisted["error"], "brain offline")


class ActivitySseTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_replays_named_events_with_ids_and_unsubscribes(self):
        activity_event = envelope(
            "sse-replay-event", kind="event", phase="perception", ts=321,
            payload={"name": "face_detected"},
        )
        unique_id = activity_event.event_id
        dashboard_module.activity_store.ingest(activity_event)

        class Request:
            async def is_disconnected(self):
                return False

        baseline = dashboard_module.activity_store.listener_count()
        response = await dashboard_module.events_stream(Request())
        iterator = response.body_iterator
        chunks = []
        try:
            for _ in range(102):
                chunk = await iterator.__anext__()
                text = chunk.decode() if isinstance(chunk, bytes) else chunk
                chunks.append(text)
                if f"id: {unique_id}" in text:
                    break
        finally:
            await iterator.aclose()
        replay = "".join(chunks)
        self.assertIn("event: event", replay)
        self.assertIn(f"id: {unique_id}", replay)
        self.assertEqual(dashboard_module.activity_store.listener_count(), baseline)


class ActivityDashboardTemplateTests(unittest.TestCase):
    def test_dashboard_uses_one_same_origin_activity_stream(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "bridge/templates/dashboard.html"
        ).read_text()
        self.assertEqual(template.count("new EventSource('/ui/activity')"), 1)
        self.assertNotIn("new EventSource('/ui/events')", template)
        self.assertNotIn("/api/perception/feed", template)
        for feed_filter in ("all", "turns", "events", "errors"):
            self.assertIn(f'data-feed-filter="{feed_filter}"', template)
        self.assertIn("error-toast", template)


if __name__ == "__main__":
    unittest.main()
