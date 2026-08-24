"""Select one of Dotty's TTS backends from a small shared state file."""

from __future__ import annotations

import importlib
import inspect
import os
from pathlib import Path

from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase

TAG = __name__
logger = setup_logging()
DEFAULT_STATE_FILE = "/var/lib/dotty-bridge/tts/provider"
DEFAULT_PROVIDER = "local_piper"
PROVIDER_MODULES = {
    "local_piper": "core.providers.tts.piper_local",
    "openai_tts": "core.providers.tts.openai_tts",
}


def read_provider(path: str | os.PathLike[str]) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return DEFAULT_PROVIDER
    return value if value in PROVIDER_MODULES else DEFAULT_PROVIDER


class TTSProvider(TTSProviderBase):
    """Facade preserving Xiaozhi's queue contract while delegating synthesis."""

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.state_file = config.get(
            "state_file",
            os.environ.get("DOTTY_TTS_STATE_FILE", DEFAULT_STATE_FILE),
        )
        provider = read_provider(self.state_file)
        provider_config = (config.get("providers") or {}).get(provider) or {}
        module_name = PROVIDER_MODULES[provider]
        module = importlib.import_module(module_name)
        self.backend = module.TTSProvider(provider_config, delete_audio_file)
        self.provider = provider
        self.interface_type = self.backend.interface_type
        self.audio_format = self.backend.audio_format
        self.tts_text_queue = self.backend.tts_text_queue
        self.tts_audio_queue = self.backend.tts_audio_queue
        self.tts_stop_request = self.backend.tts_stop_request
        logger.bind(tag=TAG).info(f"dotty_switching selected provider={provider}")

    def tts_text_priority_thread(self):
        return self.backend.tts_text_priority_thread()

    async def text_to_speak(self, text, output_file=None):
        result = self.backend.text_to_speak(text, output_file)
        return await result if inspect.isawaitable(result) else result

    async def open_audio_channels(self, conn):
        # Xiaozhi starts the facade's queue threads. The delegated backend
        # must share the same connection because its text thread reads
        # ``conn.stop_event`` and ``conn.client_abort`` directly.
        self.backend.conn = conn
        await super().open_audio_channels(conn)

    async def close(self):
        await self.backend.close()
        await super().close()

    def __getattr__(self, name):
        return getattr(self.backend, name)
