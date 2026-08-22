"""Dependency-light tests for the shared Dotty TTS provider state."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "custom-providers" / "dotty_switching" / "dotty_switching.py"


def _load_module():
    logger = types.SimpleNamespace(
        bind=lambda **_: types.SimpleNamespace(info=lambda *_a, **_k: None)
    )
    config_logger = types.ModuleType("config.logger")
    config_logger.setup_logging = lambda: logger
    config = types.ModuleType("config")
    config.__path__ = []
    config.logger = config_logger

    base = types.ModuleType("core.providers.tts.base")
    base.TTSProviderBase = object
    spec = importlib.util.spec_from_file_location("dotty_switching_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    previous = {
        name: sys.modules.get(name)
        for name in ("config", "config.logger", "core", "core.providers", "core.providers.tts", "core.providers.tts.base")
    }
    sys.modules.update(
        {
            "config": config,
            "config.logger": config_logger,
            "core": types.ModuleType("core"),
            "core.providers": types.ModuleType("core.providers"),
            "core.providers.tts": types.ModuleType("core.providers.tts"),
            "core.providers.tts.base": base,
        }
    )
    try:
        spec.loader.exec_module(module)
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    return module


class TestDottySwitchingState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def test_missing_and_invalid_state_default_to_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider"
            self.assertEqual(self.module.read_provider(path), "local_piper")
            path.write_text("invalid\n", encoding="utf-8")
            self.assertEqual(self.module.read_provider(path), "local_piper")

    def test_valid_openai_state_is_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider"
            path.write_text("OPENAI_TTS\n", encoding="utf-8")
            self.assertEqual(self.module.read_provider(path), "openai_tts")


if __name__ == "__main__":
    unittest.main()
