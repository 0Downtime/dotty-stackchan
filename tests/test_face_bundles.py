from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from bridge.face_bundles import (
    BUNDLE_BY_ID,
    BUNDLES,
    FaceBundleStore,
    effective_voice_profile,
)


class FaceBundleContractTests(unittest.TestCase):
    def test_catalog_has_four_unique_safe_bundles(self):
        self.assertEqual(
            {bundle.id for bundle in BUNDLES},
            {"classic", "crt-pixel", "aussie-host", "kid-bot"},
        )
        self.assertEqual(len({bundle.id for bundle in BUNDLES}), len(BUNDLES))
        self.assertEqual(BUNDLE_BY_ID["classic"].requested_voice_profile, "local-cori")
        self.assertTrue(BUNDLE_BY_ID["kid-bot"].enables_kid_mode)
        self.assertFalse(BUNDLE_BY_ID["aussie-host"].enables_kid_mode)

    def test_kid_mode_and_unavailable_realtime_force_local_without_losing_request(self):
        effective, warning = effective_voice_profile(
            "realtime-marin", kid_mode=True, realtime_available=True,
        )
        self.assertEqual(effective, "local-cori")
        self.assertIn("Kid Mode", warning)
        effective, warning = effective_voice_profile(
            "realtime-marin", kid_mode=False, realtime_available=False,
        )
        self.assertEqual(effective, "local-cori")
        self.assertIn("retried", warning)

    def test_per_device_state_is_atomic_isolated_and_defaultable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "face-bundles.json"
            store = FaceBundleStore(path)
            store.set_requested("dev-a", BUNDLE_BY_ID["aussie-host"], pending=True)
            store.set_requested("dev-b", BUNDLE_BY_ID["kid-bot"], pending=True)
            self.assertEqual(store.get("dev-a")["bundle_id"], "aussie-host")
            self.assertEqual(store.get("dev-b")["bundle_id"], "kid-bot")
            self.assertEqual(json.loads(path.read_text())["version"], 1)
            self.assertEqual(store.get("missing")["bundle_id"], "classic")
            self.assertEqual(list(path.parent.glob(".face-bundles.json.*")), [])

    def test_confirmation_event_marks_only_matching_face_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = FaceBundleStore(Path(temporary) / "state.json")
            store.set_requested("dev", BUNDLE_BY_ID["aussie-host"], pending=True)
            failed = store.mark_active("dev", "classic", success=False, reason="decode")
            self.assertTrue(failed["pending"])
            self.assertEqual(failed["active_face_pack_id"], "classic")
            active = store.mark_active("dev", "aussie-host", success=True)
            self.assertFalse(active["pending"])

    def test_server_interfaces_and_reconnect_reassertion_are_wired(self):
        root = Path(__file__).resolve().parents[1]
        http_server = (
            root / "custom-providers/xiaozhi-patches/http_server.py"
        ).read_text()
        websocket_server = (
            root / "custom-providers/xiaozhi-patches/websocket_server.py"
        ).read_text()
        events = (
            root / "custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py"
        ).read_text()
        self.assertIn('"/xiaozhi/admin/set-face-pack"', http_server)
        self.assertIn('"self.robot.set_face_pack"', http_server)
        self.assertIn("FaceBundleStore().get(_dotty_dev_id)", websocket_server)
        self.assertIn('event_name == "face_pack_changed"', events)


if __name__ == "__main__":
    unittest.main()
