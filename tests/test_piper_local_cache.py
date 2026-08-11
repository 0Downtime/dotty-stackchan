import importlib.util
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock


MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom-providers",
    "piper_local",
    "piper_local.py",
)


class PiperLocalCacheTests(unittest.TestCase):
    def _load_module(self):
        numpy = types.ModuleType("numpy")
        piper_voice = types.ModuleType("piper.voice")
        piper_voice.PiperVoice = MagicMock()
        piper_voice.SynthesisConfig = MagicMock()
        piper = types.ModuleType("piper")
        piper.voice = piper_voice

        scipy_signal = types.ModuleType("scipy.signal")
        scipy = types.ModuleType("scipy")
        scipy.signal = scipy_signal

        config_logger = types.ModuleType("config.logger")
        config_logger.setup_logging = MagicMock(return_value=MagicMock())
        config = types.ModuleType("config")
        config.logger = config_logger

        tts_base = types.ModuleType("core.providers.tts.base")
        tts_base.TTSProviderBase = type("TTSProviderBase", (), {})
        tts_dto = types.ModuleType("core.providers.tts.dto.dto")
        tts_dto.ContentType = MagicMock()
        tts_dto.InterfaceType = MagicMock()
        tts_dto.SentenceType = MagicMock()
        core_utils_tts = types.ModuleType("core.utils.tts")
        core_utils_tts.MarkdownCleaner = MagicMock()
        core_utils = types.ModuleType("core.utils")
        core_utils.opus_encoder_utils = MagicMock()
        core_utils.textUtils = MagicMock()
        core = types.ModuleType("core")
        core.utils = core_utils

        fake_modules = {
            "numpy": numpy,
            "piper": piper,
            "piper.voice": piper_voice,
            "scipy": scipy,
            "scipy.signal": scipy_signal,
            "config": config,
            "config.logger": config_logger,
            "core": core,
            "core.utils": core_utils,
            "core.utils.tts": core_utils_tts,
            "core.providers": types.ModuleType("core.providers"),
            "core.providers.tts": types.ModuleType("core.providers.tts"),
            "core.providers.tts.base": tts_base,
            "core.providers.tts.dto": types.ModuleType("core.providers.tts.dto"),
            "core.providers.tts.dto.dto": tts_dto,
        }
        old_modules = {name: sys.modules.get(name) for name in fake_modules}
        sys.modules.update(fake_modules)
        try:
            spec = importlib.util.spec_from_file_location(
                "piper_local_cache_test", MODULE_PATH
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            for name, old in old_modules.items():
                if old is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old

    def test_same_paths_load_voice_once_and_share_lock(self):
        module = self._load_module()
        voice = object()
        module.PiperVoice.load.return_value = voice

        with tempfile.TemporaryDirectory() as tmp:
            model = os.path.join(tmp, "voice.onnx")
            config = os.path.join(tmp, "voice.json")
            first = module._cached_voice(model, config)
            second = module._cached_voice(model, config)

        self.assertIs(first[0], voice)
        self.assertIs(first[1], second[1])
        self.assertIsInstance(first[1], type(threading.RLock()))
        module.PiperVoice.load.assert_called_once_with(model, config)

    def test_different_models_have_separate_cache_entries(self):
        module = self._load_module()
        module.PiperVoice.load.side_effect = [object(), object()]

        module._cached_voice("/models/a.onnx", "/models/a.json")
        module._cached_voice("/models/b.onnx", "/models/b.json")

        self.assertEqual(module.PiperVoice.load.call_count, 2)


if __name__ == "__main__":
    unittest.main()
