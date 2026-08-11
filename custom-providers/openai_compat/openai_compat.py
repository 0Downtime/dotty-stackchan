"""Generic OpenAI-compatible LLM provider for xiaozhi-esp32-server.

Works with any backend that exposes /v1/chat/completions:
  OpenAI, OpenRouter, Ollama, LM Studio, vLLM, etc.

Config lives in .config.yaml under LLM.OpenAICompat — see the repo's
.config.yaml for the full schema.
"""

import json
import os

import requests

from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase
from core.utils.textUtils import (
    ALLOWED_EMOJIS,
    FALLBACK_EMOJI,
    _SENTENCE_BOUNDARY,
    build_turn_suffix,
    filter_tts_stream,
)

TAG = __name__
logger = setup_logging()

KID_MODE = os.environ.get("DOTTY_KID_MODE", "true").lower() in ("1", "true", "yes")
_TURN_SUFFIX = build_turn_suffix(KID_MODE)


def _optional_bool(value, name):
    """Parse an optional boolean without silently accepting typos."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false")


def _load_persona(path):
    """Read a persona markdown file and return its contents as a string."""
    if not path:
        return ""
    resolved = os.path.expanduser(path)
    if not os.path.isabs(resolved):
        # Relative paths resolve from the xiaozhi-server working directory,
        # which is /opt/xiaozhi-esp32-server inside the container.
        resolved = os.path.join(os.getcwd(), resolved)
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.bind(tag=TAG).warning(f"Persona file not found: {resolved}")
        return ""
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"Failed to read persona file {resolved}: {exc}")
        return ""


class LLMProvider(LLMProviderBase):
    """OpenAI-compatible Chat Completions provider for xiaozhi-server.

    Speaks the standard /v1/chat/completions endpoint with streaming.
    Works out of the box with OpenAI, OpenRouter, Ollama, LM Studio,
    vLLM, and anything else that implements the same wire format.
    """

    def __init__(self, config):
        self.base_url = (
            os.environ.get("DOTTY_INFERENCE_URL") or config.get("url") or ""
        ).rstrip("/")
        if not self.base_url:
            raise ValueError(
                "OpenAICompat requires 'url' (e.g. https://api.openai.com/v1)"
            )
        self.api_key = (
            os.environ.get("DOTTY_INFERENCE_API_KEY")
            or config.get("api_key")
            or ""
        )
        self.model = os.environ.get("DOTTY_VOICE_MODEL") or config.get("model") or ""
        if not self.model:
            raise ValueError("OpenAICompat requires 'model'")
        self.max_tokens = int(config.get("max_tokens", 256))
        self.temperature = float(config.get("temperature", 0.7))
        self.timeout = float(config.get("timeout", 60))
        # Qwen-family servers such as oMLX accept this chat-template option.
        # Keep it optional so strictly OpenAI-compatible backends that reject
        # unknown request fields continue to work.  Voice deployments using a
        # reasoning model should explicitly set this to false: reasoning text
        # must never be streamed into TTS.
        enable_thinking = os.environ.get("DOTTY_VOICE_ENABLE_THINKING")
        if enable_thinking is None:
            enable_thinking = config.get("enable_thinking")
        self.enable_thinking = _optional_bool(
            enable_thinking, "DOTTY_VOICE_ENABLE_THINKING/enable_thinking"
        )
        self.offline_reply = (
            os.environ.get("DOTTY_OFFLINE_REPLY")
            or config.get("offline_reply")
            or "My local brain is unavailable right now."
        ).strip()

        # Load persona from file, fall back to inline system_prompt, then to
        # empty string (the top-level .config.yaml prompt: block will still be
        # injected by xiaozhi as a system message in the dialogue).
        persona_path = config.get("persona_file") or ""
        self._persona = _load_persona(persona_path)
        if not self._persona:
            self._persona = config.get("system_prompt") or ""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _build_messages(self, dialogue):
        """Convert the xiaozhi dialogue list into OpenAI messages.

        The dialogue already contains system/user/assistant messages from
        xiaozhi-server (including the top-level prompt: block as a system
        message).  We layer on:
          1. The persona from the markdown file (if any) as the first system
             message.
          2. The child-safety turn suffix appended to the final user message.
        """
        messages = []

        # Persona system message comes first if we have one.
        if self._persona:
            messages.append({"role": "system", "content": self._persona})

        # Append the safety/format suffix (emoji-prefix rule + kid-mode filter +
        # length limits) to the final user turn. If the dialogue has no user
        # turn — e.g. a greeter/system-only injection where the last message is
        # system or assistant — fall back to the last message regardless of role
        # so the constraints still reach the model; if the dialogue is empty,
        # carry them in a standalone system message. Without this, a no-user-turn
        # request runs completely unconstrained (no emoji, no kid-mode filter).
        last_user_idx = None
        for i, msg in enumerate(dialogue):
            if msg.get("role") == "user":
                last_user_idx = i
        suffix_idx = last_user_idx if last_user_idx is not None else len(dialogue) - 1

        for i, msg in enumerate(dialogue):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if i == suffix_idx:
                content = content + _TURN_SUFFIX
            messages.append({"role": role, "content": content})

        if suffix_idx < 0:  # empty dialogue — nothing to append to
            messages.append({"role": "system", "content": _TURN_SUFFIX})

        return messages

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _completions_url(self):
        # Support both "https://api.openai.com/v1" and
        # "https://api.openai.com/v1/" — normalize before appending.
        base = self.base_url.rstrip("/")
        # If the user already included /chat/completions, use as-is.
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _chunk_sentences(self, text):
        """Split text on sentence boundaries for TTS-friendly yielding."""
        text = (text or "").strip()
        if not text:
            return []
        pieces = [p.strip() for p in _SENTENCE_BOUNDARY.split(text)]
        return [p for p in pieces if p]

    def _offline_message(self):
        """Return the configured local-only failure response."""
        return f"{FALLBACK_EMOJI} {self.offline_reply}"

    # ------------------------------------------------------------------
    # streaming response (primary path)
    # ------------------------------------------------------------------

    def _response_stream(self, messages):
        """POST to /v1/chat/completions with stream=true, yield chunks."""
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }
        if self.enable_thinking is not None:
            payload["chat_template_kwargs"] = {
                "enable_thinking": self.enable_thinking
            }
        try:
            resp = requests.post(
                self._completions_url(),
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
                stream=True,
            )
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            logger.bind(tag=TAG).warning("OpenAICompat timeout on connect")
            yield self._offline_message()
            return
        except requests.exceptions.ConnectionError:
            logger.bind(tag=TAG).error(
                f"OpenAICompat unreachable: {self._completions_url()}"
            )
            yield self._offline_message()
            return
        except requests.exceptions.HTTPError as exc:
            logger.bind(tag=TAG).error(f"OpenAICompat HTTP error: {exc}")
            yield self._offline_message()
            return
        except Exception:
            logger.bind(tag=TAG).exception("OpenAICompat request error")
            yield self._offline_message()
            return

        # Accumulate full text so we can do emoji-prefix enforcement on the
        # first real content chunk (before yielding anything).
        full_text = []
        emoji_checked = False

        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                # SSE format: lines prefixed with "data: "
                if line.startswith("data: "):
                    data_str = line[6:]
                else:
                    # Some endpoints omit the "data: " prefix — try raw.
                    data_str = line

                if data_str.strip() == "[DONE]":
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Extract content delta from the standard SSE chunk format.
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content") or ""
                if not content:
                    continue

                full_text.append(content)

                # Emoji prefix enforcement on the first non-whitespace content.
                # Until we've seen non-whitespace, buffer rather than yield — a
                # leading whitespace-only delta must never reach TTS/firmware
                # before the emoji (the firmware parses the leading glyph into a
                # face animation, so whitespace-first breaks the contract).
                if not emoji_checked:
                    so_far = "".join(full_text).lstrip()
                    if not so_far:
                        continue  # still all whitespace — keep buffering
                    emoji_checked = True
                    if not any(so_far.startswith(e) for e in ALLOWED_EMOJIS):
                        # Prepend fallback emoji + space as a separate chunk so
                        # the face animation fires immediately.
                        yield f"{FALLBACK_EMOJI} "
                    # Flush the accumulated leading text (minus the meaningless
                    # leading whitespace) as the first real chunk.
                    yield so_far
                    continue

                yield content

        except requests.exceptions.ChunkedEncodingError:
            logger.bind(tag=TAG).warning("OpenAICompat stream interrupted")
        except Exception:
            logger.bind(tag=TAG).exception("OpenAICompat stream error")

        # If we never yielded anything, emit a fallback.
        if not full_text or not "".join(full_text).strip():
            yield f"{FALLBACK_EMOJI} (no response)"

    # ------------------------------------------------------------------
    # public interface (called by xiaozhi-server)
    # ------------------------------------------------------------------

    def _on_filter_hit(self, tier, match):
        # Local logging only — the Prometheus counter / safety ring live in
        # the bridge container, which this provider can't reach.
        logger.bind(tag=TAG).warning(
            f"OpenAICompat content-filter hit tier={tier} "
            f"pattern={match.group()!r} — turn replaced"
        )

    def response(self, session_id, dialogue, **kwargs):
        """Generate a response.  Yields string chunks.

        Uses streaming by default.  The interface matches LLMProviderBase.
        #157: in kid mode the stream is wrapped in the shared blocked-content
        filter (full-turn buffered, so nothing reaches TTS before the complete
        response is checked; a hit replaces the whole turn). The emoji-prefix
        enforcement inside _response_stream runs first, so the filter sees —
        and preserves — the leading-emoji contract.
        """
        messages = self._build_messages(dialogue)
        yield from filter_tts_stream(
            self._response_stream(messages),
            KID_MODE,
            on_hit=self._on_filter_hit,
        )
