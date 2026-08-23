"""Focused contract tests for v0.9.3 playback correlation hooks."""

from __future__ import annotations

import importlib.util
import sys
import time
import types
import unittest
from collections import deque
from enum import Enum
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVENTS: list[tuple[tuple, dict]] = []
SEND_CALLS: list[tuple] = []
WAIT_CALLS: list[object] = []


class SentenceType(Enum):
    FIRST = "first"
    MIDDLE = "middle"
    LAST = "last"


def _module(name: str, **attributes):
    mod = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(mod, key, value)
    return mod


async def _send(*args):
    SEND_CALLS.append(args)


async def _wait(conn):
    WAIT_CALLS.append(conn)


STUBS = {
    "core": _module("core"),
    "core.handle": _module("core.handle"),
    "core.handle.reportHandle": _module(
        "core.handle.reportHandle", enqueue_tts_report=lambda *_a: None,
    ),
    "core.handle.sendAudioHandle": _module(
        "core.handle.sendAudioHandle",
        sendAudioMessage=_send,
        _wait_for_audio_completion=_wait,
    ),
    "core.providers": _module("core.providers"),
    "core.providers.tts": _module("core.providers.tts"),
    "core.providers.tts.dto": _module("core.providers.tts.dto"),
    "core.providers.tts.dto.dto": _module(
        "core.providers.tts.dto.dto", SentenceType=SentenceType,
    ),
    "core.utils": _module("core.utils"),
    "core.utils.activity_telemetry": _module(
        "core.utils.activity_telemetry",
        emit_turn=lambda *a, **k: EVENTS.append((a, k)),
    ),
    "core.utils.output_counter": _module(
        "core.utils.output_counter", add_device_output=lambda *_a: None,
    ),
}

previous = {name: sys.modules.get(name) for name in STUBS}
try:
    sys.modules.update(STUBS)
    spec = importlib.util.spec_from_file_location(
        "activity_tts_under_test",
        ROOT / "custom-providers/xiaozhi-patches/activity_tts.py",
    )
    assert spec is not None and spec.loader is not None
    activity_tts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(activity_tts)
finally:
    for name, old in previous.items():
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


class ActivityPlaybackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        EVENTS.clear()
        SEND_CALLS.clear()
        WAIT_CALLS.clear()
        self.provider = activity_tts.ActivityPlaybackMixin()
        self.provider.conn = types.SimpleNamespace(
            _dotty_active_turn_id="turn-1",
            _dotty_activity_start_ts=time.time() - 0.05,
            session_id="session-1",
            device_id="device-1",
        )

    def test_sentence_binding_and_tts_start_preserve_turn_id(self):
        self.provider.activity_bind_sentence("sentence-1")
        self.provider.activity_tts_started("sentence-1")
        self.provider.activity_tts_started("sentence-1")
        self.assertEqual(len(EVENTS), 1)
        self.assertEqual(EVENTS[0][0][:2], ("tts_started", "turn-1"))
        self.assertEqual(EVENTS[0][1]["source"], "tts")

    def test_tts_failure_marks_sentence_terminal(self):
        self.provider.activity_bind_sentence("sentence-1")
        self.provider.activity_tts_failed("sentence-1", RuntimeError("offline"))
        context = self.provider._activity_ctx("sentence-1")
        self.assertEqual(context["terminal"], "failed")
        self.assertEqual(EVENTS[0][0][:2], ("failed", "turn-1"))

    def test_pending_turn_fifo_correlates_sentences_across_barge_in(self):
        self.provider.conn._dotty_pending_activity_turns = deque([
            ("turn-old", 10.0), ("turn-new", 20.0),
        ])
        self.provider.activity_bind_sentence("sentence-old")
        self.provider.activity_bind_sentence("sentence-new")
        self.assertEqual(
            self.provider._activity_ctx("sentence-old")["turn_id"], "turn-old",
        )
        self.assertEqual(
            self.provider._activity_ctx("sentence-new")["turn_id"], "turn-new",
        )

    def test_uninstrumented_sentence_does_not_reuse_previous_active_turn(self):
        self.provider.conn._dotty_pending_activity_turns = deque()
        self.provider.activity_bind_sentence("proactive-sentence")
        self.assertIsNone(
            self.provider._activity_ctx("proactive-sentence")["turn_id"],
        )

    async def test_last_frame_waits_for_upstream_rate_controller_completion(self):
        await self.provider._activity_send_audio(
            SentenceType.LAST, [], None, "sentence-1",
        )
        self.assertEqual(SEND_CALLS[0][1:], (SentenceType.LAST, [], None, "sentence-1"))
        self.assertEqual(WAIT_CALLS, [self.provider.conn])

    async def test_middle_frame_does_not_wait_for_completion(self):
        await self.provider._activity_send_audio(
            SentenceType.MIDDLE, b"opus", "hello", "sentence-1",
        )
        self.assertEqual(WAIT_CALLS, [])


if __name__ == "__main__":
    unittest.main()
