"""Focused contract tests for the OpenAI TTS adapter.

The repository's developer Python does not install the Xiaozhi runtime
dependencies, so these tests provide narrow import stubs and exercise the
provider's pure request/audio boundary without contacting OpenAI.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "custom-providers" / "openai_tts" / "openai_tts.py"


def _install_runtime_stubs() -> None:
    logger = types.SimpleNamespace(
        bind=lambda **_: types.SimpleNamespace(
            info=lambda *_a, **_k: None,
            warning=lambda *_a, **_k: None,
            error=lambda *_a, **_k: None,
        )
    )
    config_logger = types.ModuleType("config.logger")
    config_logger.setup_logging = lambda: logger
    config = types.ModuleType("config")
    config.__path__ = []
    config.logger = config_logger

    class Base:
        def __init__(self, config, delete_audio_file):
            self.config = config
            self.delete_audio_file = delete_audio_file
            self.tts_text_queue = None
            self.tts_audio_queue = types.SimpleNamespace(put=lambda *_: None)
            self.tts_stop_request = False
            self.processed_chars = 0
            self.tts_text_buff = []

        def handle_opus(self, data):
            self.tts_audio_queue.put(("middle", [data], None))

        def _process_before_stop_play_files(self):
            self.tts_audio_queue.put(("last", [], None))

        async def close(self):
            return None

    tts_base = types.ModuleType("core.providers.tts.base")
    tts_base.TTSProviderBase = Base
    dto = types.ModuleType("core.providers.tts.dto.dto")
    dto.ContentType = types.SimpleNamespace(TEXT="text", FILE="file")
    dto.InterfaceType = types.SimpleNamespace(SINGLE_STREAM="single_stream")
    dto.SentenceType = types.SimpleNamespace(FIRST="first", MIDDLE="middle", LAST="last")
    opus = types.ModuleType("core.utils.opus_encoder_utils")
    class FakeOpusEncoder:
        def __init__(self, **_kwargs):
            pass

        def encode_pcm_to_opus_stream(self, _pcm, end_of_stream, callback):
            callback(b"opus-frame")

        def close(self):
            return None

    opus.OpusEncoderUtils = FakeOpusEncoder
    utils = types.ModuleType("core.utils")
    utils.opus_encoder_utils = opus
    utils.textUtils = types.SimpleNamespace(
        get_string_no_punctuation_or_emoji=lambda value: value
    )
    tts_utils = types.ModuleType("core.utils.tts")
    tts_utils.MarkdownCleaner = types.SimpleNamespace(clean_markdown=lambda value: value)

    numpy = types.ModuleType("numpy")
    scipy = types.ModuleType("scipy")
    scipy.signal = types.SimpleNamespace()

    modules = {
        "config": config,
        "config.logger": config_logger,
        "core": types.ModuleType("core"),
        "core.providers": types.ModuleType("core.providers"),
        "core.providers.tts": types.ModuleType("core.providers.tts"),
        "core.providers.tts.base": tts_base,
        "core.providers.tts.dto": types.ModuleType("core.providers.tts.dto"),
        "core.providers.tts.dto.dto": dto,
        "core.utils": utils,
        "core.utils.opus_encoder_utils": opus,
        "core.utils.tts": tts_utils,
        "numpy": numpy,
        "scipy": scipy,
    }
    sys.modules.update(modules)


def _load_module():
    _install_runtime_stubs()
    spec = importlib.util.spec_from_file_location("test_openai_tts_module", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class TestOpenAITTS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def test_request_uses_openai_speech_contract_without_logging_key(self):
        captured = {}

        def urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response(b"\x01\x00" * 240)

        body = self.module.request_speech_pcm(
            "hello",
            api_key="secret-that-must-not-be-logged",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini-tts",
            voice="coral",
            speed=1.25,
            urlopen=urlopen,
        )
        self.assertEqual(body, b"\x01\x00" * 240)
        self.assertEqual(captured["timeout"], 30.0)
        self.assertEqual(captured["request"].full_url, "https://api.openai.com/v1/audio/speech")
        payload = json.loads(captured["request"].data)
        self.assertEqual(payload["model"], "gpt-4o-mini-tts")
        self.assertEqual(payload["voice"], "coral")
        self.assertEqual(payload["response_format"], "pcm")
        self.assertEqual(payload["speed"], 1.25)
        self.assertNotIn("secret-that-must-not-be-logged", repr(captured))

    def test_normalize_pcm_keeps_24khz_pcm_unchanged(self):
        pcm = b"\x01\x00\x02\x00"
        self.assertEqual(self.module.normalize_pcm(pcm), pcm)

    def test_normalize_pcm_rejects_odd_byte_count(self):
        with self.assertRaisesRegex(ValueError, "odd byte length"):
            self.module.normalize_pcm(b"\x00")

    def test_provider_requires_environment_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "OPENAI_TTS_API_KEY"):
                self.module.TTSProvider({}, False)

    def test_playback_emits_first_and_middle_frames_on_success(self):
        events = []

        class Queue:
            def put(self, event):
                events.append(event)

        with patch.dict(os.environ, {"OPENAI_TTS_API_KEY": "test-key"}, clear=True):
            provider = self.module.TTSProvider({}, False)
            provider.tts_audio_queue = Queue()
            with patch.object(self.module, "request_speech_pcm", return_value=b"\x00\x00" * 24000):
                provider.to_tts_single_stream("hello", is_last=True)

        self.assertEqual(events[0], ("first", [], "hello"))
        self.assertGreaterEqual(sum(event[0] == "middle" for event in events), 1)

    def test_playback_emits_last_once_on_provider_failure(self):
        events = []

        class Queue:
            def put(self, event):
                events.append(event)

        with patch.dict(os.environ, {"OPENAI_TTS_API_KEY": "test-key"}, clear=True):
            provider = self.module.TTSProvider({}, False)
            provider.tts_audio_queue = Queue()
            with patch.object(
                self.module,
                "request_speech_pcm",
                side_effect=RuntimeError("provider unavailable"),
            ):
                provider.to_tts_single_stream("hello", is_last=True)

        self.assertEqual(events[0], ("first", [], "hello"))
        self.assertEqual(events[-1], ("last", [], None))
        self.assertEqual(sum(event[0] == "last" for event in events), 1)


if __name__ == "__main__":
    unittest.main()
