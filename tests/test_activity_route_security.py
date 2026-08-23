"""Localhost and shared-token enforcement for activity ingestion."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


state_dir = Path(tempfile.mkdtemp(prefix="dotty-activity-route-state-"))
os.environ.setdefault("DOTTY_KID_MODE_STATE", str(state_dir / "kid-mode"))
os.environ.setdefault("DOTTY_SMART_MODE_STATE", str(state_dir / "smart-mode"))

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("activity_route_app", root / "bridge.py")
assert spec is not None and spec.loader is not None
bridge_app = importlib.util.module_from_spec(spec)
sys.modules["activity_route_app"] = bridge_app
spec.loader.exec_module(bridge_app)

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class ActivityRouteSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_token = bridge_app._ADMIN_TOKEN
        bridge_app._ADMIN_TOKEN = "shared-token"
        self.client = TestClient(bridge_app.app)

    def tearDown(self) -> None:
        bridge_app._ADMIN_TOKEN = self.old_token
        bridge_app.app.dependency_overrides.clear()

    def _payload(self):
        return {
            "schema_version": 1,
            "event_id": "22222222-2222-4222-8222-222222222222",
            "ts": 100.0,
            "source": "firmware",
            "kind": "event",
            "phase": "perception",
            "turn_id": None,
            "session_id": None,
            "device_id": "device-1",
            "payload": {"name": "face_detected", "data": {}},
        }

    def test_missing_or_wrong_token_is_rejected(self):
        self.assertEqual(
            self.client.post("/admin/activity", json=self._payload()).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/admin/activity", json=self._payload(),
                headers={"X-Admin-Token": "wrong"},
            ).status_code,
            401,
        )

    def test_matching_token_ingests_and_deduplicates(self):
        headers = {"X-Admin-Token": "shared-token"}
        first = self.client.post("/admin/activity", json=self._payload(), headers=headers)
        second = self.client.post("/admin/activity", json=self._payload(), headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(second.json()["duplicate"])

    def test_non_loopback_request_is_rejected(self):
        bridge_app._ADMIN_TOKEN = ""
        request = types.SimpleNamespace(
            client=types.SimpleNamespace(host="192.0.2.10"),
            headers={},
        )
        with self.assertRaises(HTTPException) as caught:
            bridge_app._admin_require_activity_access(request)
        self.assertEqual(caught.exception.status_code, 403)

    def test_loopback_is_allowed_when_token_is_not_configured(self):
        bridge_app._ADMIN_TOKEN = ""
        request = types.SimpleNamespace(
            client=types.SimpleNamespace(host="127.0.0.1"),
            headers={},
        )
        bridge_app._admin_require_activity_access(request)


if __name__ == "__main__":
    unittest.main()
