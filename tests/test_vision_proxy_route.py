"""Tests for the LAN-to-private vision relay on xiaozhi HTTP port 8003."""

import asyncio
import importlib.util
import json
import os
import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch


for name in (
    "config",
    "config.logger",
    "core",
    "core.api",
    "core.api.ota_handler",
    "core.api.vision_handler",
    "core.portal_bridge",
    "core.utils",
):
    sys.modules.setdefault(name, MagicMock())
sys.modules["config.logger"].setup_logging = lambda: MagicMock()
sys.modules["core.portal_bridge"].active_connections = {}

root = pathlib.Path(__file__).parent.parent
device_command_path = (
    root / "custom-providers" / "xiaozhi-patches" / "device_command.py"
)
device_spec = importlib.util.spec_from_file_location(
    "core.utils.device_command", device_command_path
)
device_module = importlib.util.module_from_spec(device_spec)
device_spec.loader.exec_module(device_module)
sys.modules["core.utils"].device_command = device_module
sys.modules["core.utils.device_command"] = device_module

server_path = root / "custom-providers" / "xiaozhi-patches" / "http_server.py"
server_spec = importlib.util.spec_from_file_location(
    "http_server_vision_proxy_under_test", server_path
)
server_module = importlib.util.module_from_spec(server_spec)
server_spec.loader.exec_module(server_module)


class FakeRequest:
    def __init__(self, body=b"jpeg", headers=None):
        self.body = body
        self.headers = headers or {}

    async def read(self):
        return self.body


class FakeUpstream:
    status = 201
    headers = {"Content-Type": "application/json"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self):
        return b'{"description":"ready"}'


class FakeSession:
    last_post = None

    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, target, *, data, headers):
        type(self).last_post = (target, data, headers)
        return FakeUpstream()


class VisionProxyRouteTests(unittest.TestCase):
    def setUp(self):
        self.previous = os.environ.get("DOTTY_VISION_PROXY_URL")
        self.server = server_module.SimpleHttpServer({"server": {}})

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("DOTTY_VISION_PROXY_URL", None)
        else:
            os.environ["DOTTY_VISION_PROXY_URL"] = self.previous

    def test_missing_target_is_503(self):
        os.environ.pop("DOTTY_VISION_PROXY_URL", None)
        response = asyncio.run(self.server._dotty_vision_proxy(FakeRequest()))
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.body)["error"], "vision proxy is not configured")

    def test_relays_body_and_only_safe_headers(self):
        os.environ["DOTTY_VISION_PROXY_URL"] = "http://behaviour:8090/api/vision/explain"
        request = FakeRequest(
            headers={
                "Content-Type": "multipart/form-data; boundary=x",
                "Device-Id": "device-1",
                "Client-Id": "client-1",
                "Authorization": "Bearer do-not-forward",
            }
        )
        with patch.object(server_module, "ClientSession", FakeSession):
            response = asyncio.run(self.server._dotty_vision_proxy(request))

        self.assertEqual(response.status, 201)
        self.assertEqual(response.body, b'{"description":"ready"}')
        target, body, headers = FakeSession.last_post
        self.assertEqual(target, os.environ["DOTTY_VISION_PROXY_URL"])
        self.assertEqual(body, b"jpeg")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["Device-Id"], "device-1")


if __name__ == "__main__":
    unittest.main()
