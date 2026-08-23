"""OpenAI Audio Speech TTS provider for Xiaozhi.

The provider requests raw PCM from ``/v1/audio/speech`` and feeds it through
the same 24 kHz Opus framing path used by the local Piper provider.  API
credentials are read only from the process environment; they are never read
from the YAML config or logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import traceback
import urllib.error
import urllib.request
from math import gcd

import numpy as np
from scipy import signal

from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import ContentType, InterfaceType, SentenceType
from core.utils import opus_encoder_utils, textUtils
from core.utils.tts import MarkdownCleaner

TAG = __name__
logger = setup_logging()

TARGET_RATE = 24000
FRAME_MS = 60
FRAME_BYTES = TARGET_RATE * FRAME_MS // 1000 * 2
DEFAULT_MODEL = "gpt-4o-mini-tts"
DEFAULT_VOICE = "coral"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _env_float(name: str, default: float) -> float:
    try:
        return min(4.0, max(0.25, float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def normalize_pcm(pcm: bytes, source_rate: int = TARGET_RATE) -> bytes:
    """Return mono signed-16 PCM at the StackChan's 24 kHz output rate."""
    if len(pcm) % 2:
        raise ValueError("openai_tts: PCM response has an odd byte length")
    if source_rate <= 0:
        raise ValueError(f"openai_tts: invalid source sample rate {source_rate}")
    if source_rate == TARGET_RATE:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16)
    factor = gcd(source_rate, TARGET_RATE)
    resampled = signal.resample_poly(
        samples,
        TARGET_RATE // factor,
        source_rate // factor,
    )
    return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()


def request_speech_pcm(
    text: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    voice: str,
    speed: float,
    timeout: float = 30.0,
    urlopen=urllib.request.urlopen,
) -> bytes:
    """Call OpenAI's speech endpoint and return its PCM body."""
    endpoint = f"{base_url.rstrip('/')}/audio/speech"
    payload = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "pcm",
        "speed": speed,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "audio/pcm",
            "User-Agent": "dotty-stackchan-openai-tts/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(512).decode("utf-8", errors="replace")
        raise RuntimeError(
            f"OpenAI TTS HTTP {exc.code}: {detail[:200]}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"OpenAI TTS request failed: {exc}") from exc
    if not body:
        raise RuntimeError("OpenAI TTS returned an empty audio response")
    return body


class TTSProvider(TTSProviderBase):
    """Sentence-buffered OpenAI TTS with Xiaozhi-compatible Opus output."""

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.interface_type = InterfaceType.SINGLE_STREAM
        self.audio_format = "pcm"
        self.before_stop_play_files = []
        # A dedicated key is preferred; the existing OpenAI deployment key is
        # accepted as an environment-only fallback so installations that
        # already use the Realtime path do not need to duplicate a secret.
        self.api_key = (
            os.environ.get("OPENAI_TTS_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        )
        if not self.api_key:
            raise ValueError("openai_tts: OPENAI_TTS_API_KEY is not configured")
        self.model = os.environ.get(
            "OPENAI_TTS_MODEL", DEFAULT_MODEL
        ).strip() or DEFAULT_MODEL
        self.voice = os.environ.get(
            "OPENAI_TTS_VOICE", DEFAULT_VOICE
        ).strip() or DEFAULT_VOICE
        self.base_url = os.environ.get(
            "OPENAI_TTS_BASE_URL", DEFAULT_BASE_URL
        ).strip() or DEFAULT_BASE_URL
        self.speed = _env_float("OPENAI_TTS_SPEED", 1.0)
        self.timeout = max(
            1.0, float(os.environ.get("OPENAI_TTS_TIMEOUT_SECONDS", "30"))
        )
        self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=TARGET_RATE, channels=1, frame_size_ms=FRAME_MS
        )
        self.pcm_buffer = bytearray()
        logger.bind(tag=TAG).info(
            f"openai_tts loaded model={self.model!r} voice={self.voice!r} "
            f"speed={self.speed}"
        )

    def tts_text_priority_thread(self):
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                if message.sentence_type == SentenceType.FIRST:
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    self.before_stop_play_files.clear()
                elif ContentType.TEXT == message.content_type:
                    self.tts_text_buff.append(message.content_detail)
                    segment_text = self._get_segment_text()
                    if segment_text:
                        self.to_tts_single_stream(segment_text)
                elif ContentType.FILE == message.content_type:
                    if message.content_file and os.path.exists(message.content_file):
                        self._process_audio_file_stream(
                            message.content_file,
                            callback=lambda audio_data: self.handle_audio_file(
                                audio_data, message.content_detail
                            ),
                        )

                if message.sentence_type == SentenceType.LAST:
                    self._process_remaining_text_stream(True)
            except queue.Empty:
                continue
            except Exception as exc:
                logger.bind(tag=TAG).error(
                    f"OpenAI TTS text thread error: {exc}\n{traceback.format_exc()}"
                )

    def _process_remaining_text_stream(self, is_last=False):
        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars :]
        if remaining_text:
            segment_text = textUtils.get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                self.to_tts_single_stream(segment_text, is_last)
                self.processed_chars += len(full_text)
            else:
                self._process_before_stop_play_files()
        else:
            self._process_before_stop_play_files()

    def to_tts_single_stream(self, text, is_last=False):
        text = MarkdownCleaner.clean_markdown(text)
        try:
            asyncio.run(self.text_to_speak(text, is_last))
        except Exception as exc:
            logger.bind(tag=TAG).error(
                "OpenAI TTS synthesis failed; no local fallback: %s", exc
            )
            self.tts_audio_queue.put((SentenceType.LAST, [], None))
        return None

    async def text_to_speak(self, text, is_last):
        self.pcm_buffer.clear()
        self.tts_audio_queue.put((SentenceType.FIRST, [], text))
        try:
            raw_pcm = await asyncio.to_thread(
                request_speech_pcm,
                text,
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                voice=self.voice,
                speed=self.speed,
                timeout=self.timeout,
            )
            self.pcm_buffer.extend(normalize_pcm(raw_pcm))
            while len(self.pcm_buffer) >= FRAME_BYTES:
                frame = bytes(self.pcm_buffer[:FRAME_BYTES])
                del self.pcm_buffer[:FRAME_BYTES]
                self.opus_encoder.encode_pcm_to_opus_stream(
                    frame, end_of_stream=False, callback=self.handle_opus
                )
            if self.pcm_buffer:
                self.opus_encoder.encode_pcm_to_opus_stream(
                    bytes(self.pcm_buffer),
                    end_of_stream=True,
                    callback=self.handle_opus,
                )
                self.pcm_buffer.clear()
            if is_last:
                self._process_before_stop_play_files()
        except Exception:
            # The outer wrapper emits the single terminal marker after
            # recording the provider failure.  Keeping that responsibility in
            # one place avoids duplicate LAST events on failed synthesis.
            raise

    async def close(self):
        await super().close()
        if hasattr(self, "opus_encoder"):
            self.opus_encoder.close()
