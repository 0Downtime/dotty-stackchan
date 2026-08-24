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

        def _base_get_segment_text(_self):
            return None

        tts_base.TTSProviderBase = type(
            "TTSProviderBase", (), {"_get_segment_text": _base_get_segment_text}
        )
        tts_dto = types.ModuleType("core.providers.tts.dto.dto")
        tts_dto.ContentType = MagicMock()
        tts_dto.InterfaceType = MagicMock()
        tts_dto.SentenceType = MagicMock()
        core_utils_tts = types.ModuleType("core.utils.tts")
        core_utils_tts.MarkdownCleaner = MagicMock()
        activity_tts = types.ModuleType("core.utils.activity_tts")
        activity_tts.ActivityPlaybackMixin = type("ActivityPlaybackMixin", (), {})
        core_utils = types.ModuleType("core.utils")
        core_utils.opus_encoder_utils = MagicMock()
        core_utils.textUtils = MagicMock()
        core_utils.textUtils.get_string_no_punctuation_or_emoji.side_effect = (
            lambda value: value.strip()
        )
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
            "core.utils.activity_tts": activity_tts,
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

    def test_long_unpunctuated_text_flushes_at_word_boundary(self):
        module = self._load_module()
        provider = module.TTSProvider.__new__(module.TTSProvider)
        provider.tts_text_buff = [
            "😊 This is a deliberately long response without punctuation "
            "so first audio can start"
        ]
        provider.processed_chars = 0
        provider.is_first_sentence = True

        segment = provider._get_segment_text()

        self.assertEqual(
            segment,
            "😊 This is a deliberately long response without",
        )
        self.assertEqual(
            provider.tts_text_buff[0][provider.processed_chars :].lstrip(),
            "punctuation so first audio can start",
        )
        self.assertFalse(provider.is_first_sentence)

    def test_short_unpunctuated_text_waits_for_more_input(self):
        module = self._load_module()
        provider = module.TTSProvider.__new__(module.TTSProvider)
        provider.tts_text_buff = ["😊 A short unfinished thought"]
        provider.processed_chars = 0
        provider.is_first_sentence = True

        self.assertIsNone(provider._get_segment_text())
        self.assertEqual(provider.processed_chars, 0)


if __name__ == "__main__":
    unittest.main()
