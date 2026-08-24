"""Focused unit coverage for dashboard helpers and static endpoints.

The dashboard is a large server-rendered router, so this suite starts with
the pure helpers, auth/proxy boundaries, and static browser assets. It keeps
those contracts testable without starting the bridge lifespan or contacting
xiaozhi-server/dotty-behaviour.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from starlette.requests import Request


_state_dir = Path(tempfile.mkdtemp(prefix="dotty-dashboard-helper-test-"))
os.environ.setdefault("DOTTY_KID_MODE_STATE", str(_state_dir / "kid-mode"))
os.environ.setdefault("DOTTY_SMART_MODE_STATE", str(_state_dir / "smart-mode"))
os.environ.setdefault("CONVO_LOG_DIR", str(_state_dir / "logs"))
os.environ.setdefault("IDLE_PHOTOGRAPHER_ENABLED", "0")
os.environ.setdefault("DREAMER_ENABLED", "0")
os.environ.setdefault("DANCE_REFLECTOR_ENABLED", "0")
os.environ.setdefault("CALENDAR_IDS", "")
os.environ.setdefault("ZEROCLAW_BIN", "/bin/true")

_repo_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bridge_app_helpers", _repo_root / "bridge.py")
assert _spec is not None and _spec.loader is not None
bridge_app = importlib.util.module_from_spec(_spec)
sys.modules["bridge_app_helpers"] = bridge_app
_spec.loader.exec_module(bridge_app)

import bridge.dashboard as dash  # noqa: E402


def _request(path: str = "/ui") -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
    })


class DashboardTextHelperTests(unittest.TestCase):
    def test_humanize_age_boundaries(self):
        self.assertEqual(dash._humanize_age(0), "0s")
        self.assertEqual(dash._humanize_age(59), "59s")
        self.assertEqual(dash._humanize_age(60), "1m")
        self.assertEqual(dash._humanize_age(3599), "59m")
        self.assertEqual(dash._humanize_age(3600), "1h")
        self.assertEqual(dash._humanize_age(86399), "23h")
        self.assertEqual(dash._humanize_age(86400), "1d")

    def test_clean_request_text_without_marker_is_unchanged(self):
        self.assertEqual(dash._clean_request_text("hello"), "hello")
        self.assertEqual(dash._clean_request_text(""), "")

    def test_clean_request_text_extracts_raw_user_text(self):
        self.assertEqual(
            dash._clean_request_text("[Context] hidden\n[User]  hello Dotty  "),
            "hello Dotty",
        )

    def test_clean_request_text_extracts_json_content(self):
        payload = json.dumps({"content": "  hello from JSON "})
        self.assertEqual(dash._clean_request_text(f"[User] {payload}"), "hello from JSON")

    def test_clean_request_text_keeps_invalid_json_as_text(self):
        self.assertEqual(dash._clean_request_text("[User] {not json}"), "{not json}")

    def test_parse_ts_accepts_iso_and_rejects_invalid_values(self):
        self.assertIsNotNone(dash._parse_ts("2026-08-24T12:00:00Z"))
        self.assertIsNone(dash._parse_ts(""))
        self.assertIsNone(dash._parse_ts("not-a-timestamp"))

    def test_short_model_strips_provider(self):
        self.assertEqual(dash._short_model("provider/model"), "model")
        self.assertEqual(dash._short_model("model"), "model")
        self.assertEqual(dash._short_model(""), "")

    def test_host_detail_label_does_not_claim_model_swap(self):
        self.assertIn("PiVoiceLLM", dash._host_detail_llm_label())
        self.assertIn("pending", dash._host_detail_llm_label(True))


class DashboardAuthAndProxyTests(unittest.TestCase):
    def test_auth_is_disabled_when_credentials_are_unconfigured(self):
        with patch.object(dash, "_DASHBOARD_USER", ""), patch.object(dash, "_DASHBOARD_PASS", ""):
            self.assertIsNone(dash._verify_dashboard_auth(None))

    def test_auth_rejects_missing_credentials(self):
        with patch.object(dash, "_DASHBOARD_USER", "dotty"), patch.object(dash, "_DASHBOARD_PASS", "secret"):
            with self.assertRaises(HTTPException) as ctx:
                dash._verify_dashboard_auth(None)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("WWW-Authenticate", ctx.exception.headers)

    def test_auth_rejects_wrong_credentials(self):
        credentials = HTTPBasicCredentials(username="dotty", password="wrong")
        with patch.object(dash, "_DASHBOARD_USER", "dotty"), patch.object(dash, "_DASHBOARD_PASS", "secret"):
            with self.assertRaises(HTTPException) as ctx:
                dash._verify_dashboard_auth(credentials)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_auth_accepts_matching_credentials(self):
        credentials = HTTPBasicCredentials(username="dotty", password="secret")
        with patch.object(dash, "_DASHBOARD_USER", "dotty"), patch.object(dash, "_DASHBOARD_PASS", "secret"):
            self.assertIsNone(dash._verify_dashboard_auth(credentials))

    def test_admin_headers_are_empty_without_token(self):
        with patch.object(dash, "_ADMIN_TOKEN", ""):
            self.assertEqual(dash._xiaozhi_admin_headers(), {})

    def test_admin_headers_include_token_when_configured(self):
        with patch.object(dash, "_ADMIN_TOKEN", "test-token"):
            self.assertEqual(dash._xiaozhi_admin_headers(), {"X-Admin-Token": "test-token"})

    def test_face_admin_request_requires_host(self):
        with patch.object(dash, "XIAOZHI_HOST", ""):
            status, body = dash._face_admin_request("GET", "face-pack-status")
        self.assertEqual(status, 503)
        self.assertIn("not configured", body["error"])

    def test_face_admin_request_returns_json_response(self):
        response = Mock(status_code=200)
        response.json.return_value = {"active_face_pack_id": "classic"}
        with patch.object(dash, "XIAOZHI_HOST", "server"), patch.object(
            dash.requests, "request", return_value=response,
        ) as request:
            status, body = dash._face_admin_request("GET", "face-pack-status")
        self.assertEqual((status, body), (200, {"active_face_pack_id": "classic"}))
        request.assert_called_once()

    def test_fetch_robot_photo_maps_not_found(self):
        response = Mock(status_code=404, ok=False)
        with patch.object(dash.requests, "get", return_value=response):
            with self.assertRaises(HTTPException) as ctx:
                dash._fetch_robot_photo("dotty")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_fetch_robot_photo_maps_transport_failure(self):
        with patch.object(
            dash.requests, "get", side_effect=dash.requests.RequestException("offline"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                dash._fetch_robot_photo("dotty")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_fetch_robot_photo_returns_no_store_jpeg(self):
        response = Mock(status_code=200, ok=True, content=b"jpeg")
        response.headers = {"content-type": "image/jpeg"}
        with patch.object(dash.requests, "get", return_value=response):
            result = dash._fetch_robot_photo("dotty")
        self.assertEqual(result.body, b"jpeg")
        self.assertEqual(result.media_type, "image/jpeg")
        self.assertEqual(result.headers["cache-control"], "no-store")


class DashboardStaticEndpointTests(unittest.TestCase):
    def test_manifest_has_matching_scope_and_start_url(self):
        result = asyncio.run(dash.manifest())
        body = json.loads(result.body)
        self.assertEqual(body["start_url"], "/ui")
        self.assertEqual(body["scope"], "/ui")
        self.assertEqual(body["short_name"], "Dotty")

    def test_icon_and_hero_are_svg(self):
        icon = asyncio.run(dash.icon())
        hero = asyncio.run(dash.hero())
        self.assertEqual(icon.media_type, "image/svg+xml")
        self.assertEqual(hero.media_type, "image/svg+xml")
        self.assertIn(b"<svg", icon.body)
        self.assertIn(b"<svg", hero.body)

    def test_apple_touch_icon_is_png_when_bundled(self):
        with patch.object(dash, "_APPLE_ICON_BYTES", b"png"):
            result = asyncio.run(dash.apple_touch_icon())
        self.assertEqual(result.media_type, "image/png")
        self.assertEqual(result.body, b"png")

    def test_apple_touch_icon_reports_missing_asset(self):
        with patch.object(dash, "_APPLE_ICON_BYTES", b""):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(dash.apple_touch_icon())
        self.assertEqual(ctx.exception.status_code, 404)

    def test_version_chip_uses_static_context(self):
        request = _request("/ui/version-chip")
        with patch.object(dash, "_static_chip_context", return_value={"installed_display": "vtest"}):
            result = asyncio.run(dash.version_chip(request))
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"vtest", result.body)


class DashboardSystemHelperTests(unittest.TestCase):
    def test_read_first_line_returns_empty_for_missing_file(self):
        self.assertEqual(dash._read_first_line("/path/that/does/not/exist"), "")

    def test_cpu_temp_and_uptime_parse_values(self):
        with patch.object(dash, "_read_first_line", side_effect=["42000", "123.5"]):
            self.assertEqual(dash._cpu_temp_c(), 42.0)
            self.assertEqual(dash._proc_uptime_sec(), 123.5)

    def test_cpu_temp_and_uptime_fail_closed_on_bad_values(self):
        with patch.object(dash, "_read_first_line", side_effect=["bad", ""]):
            self.assertIsNone(dash._cpu_temp_c())
            self.assertIsNone(dash._proc_uptime_sec())


if __name__ == "__main__":
    unittest.main()
