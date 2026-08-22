"""Non-blocking xiaozhi telemetry queue behavior."""

from __future__ import annotations

import importlib.util
import os
import queue
import unittest
from pathlib import Path
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / "custom-providers/xiaozhi-patches/activity_telemetry.py"
spec = importlib.util.spec_from_file_location("activity_telemetry_under_test", path)
assert spec is not None and spec.loader is not None
telemetry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(telemetry)


class ActivityTelemetryTests(unittest.TestCase):
    def test_disabled_sender_is_a_noop(self):
        with patch.dict(os.environ, {"DOTTY_ACTIVITY_ENABLED": "false"}):
            self.assertFalse(telemetry.emit_turn("model_started", "turn-1"))

    def test_full_queue_drops_immediately(self):
        original_queue = telemetry._QUEUE
        original_worker = telemetry._ensure_worker
        telemetry._QUEUE = queue.Queue(maxsize=1)
        telemetry._ensure_worker = lambda: None
        self.addCleanup(lambda: setattr(telemetry, "_QUEUE", original_queue))
        self.addCleanup(lambda: setattr(telemetry, "_ensure_worker", original_worker))
        with patch.dict(os.environ, {"DOTTY_ACTIVITY_ENABLED": "true"}):
            self.assertTrue(telemetry.emit_turn("model_started", "turn-1"))
            self.assertFalse(telemetry.emit_turn("first_text", "turn-1"))

    def test_missing_turn_id_is_never_enqueued(self):
        with patch.dict(os.environ, {"DOTTY_ACTIVITY_ENABLED": "true"}):
            self.assertFalse(telemetry.emit_turn("failed", None))


if __name__ == "__main__":
    unittest.main()
