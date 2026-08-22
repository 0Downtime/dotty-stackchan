"""PiVoiceLLM — xiaozhi-server LLM provider that routes voice turns
through the dotty-pi container instead of bridge.py.

Unlike a plain OpenAI-style provider that parses `tool_calls` and
dispatches each one xiaozhi-side, PiVoiceLLM doesn't do that: pi itself
owns the agent loop and the tool dispatch happens inside the dotty-pi-ext
extension. From xiaozhi's perspective this provider is a much simpler
shape — translate the dialogue into a single pi prompt, stream pi's
user-visible text chunks back to TTS, done.

Per #36 Step-5 contract:
  - PiVoiceLLM owns ONE PiClient — long-lived across all turns.
  - Between turns we issue `new_session` to reset pi's working state
    without re-spawning the process.
  - Thinking deltas + extension UI requests are filtered inside
    PiClient (see pi_client.py) — by the time text reaches `response()`
    only TTS-bound chunks remain.

Configuration via `data/.config.yaml`:

```yaml
selected_module:
  LLM: PiVoiceLLM

LLM:
  PiVoiceLLM:
    type: pi_voice
    container_name: dotty-pi
    # Optional — flags appended after the default ones in PiClient.
    extra_pi_flags: ""
```
"""

from __future__ import annotations

import json
import os
import time
import unicodedata
from pathlib import Path
from typing import Iterator

from .pi_client import PiClient, PiClientError, make_default_pi_client

try:
    from core.utils.activity_telemetry import emit_turn as _emit_activity_turn  # type: ignore
except ImportError:  # pragma: no cover - workstation unit tests
    def _emit_activity_turn(*args, **kwargs):
        return False


try:
    from config.logger import setup_logging  # type: ignore
    from core.providers.llm.base import LLMProviderBase  # type: ignore
except ImportError:  # pragma: no cover — only on dev workstation
    # Provide tiny stand-ins so this file imports cleanly during
    # extension-side unit tests. xiaozhi-server overrides both.
    class LLMProviderBase:  # type: ignore[no-redef]
        pass

    def setup_logging():  # type: ignore[no-redef]
        import logging
        return logging.getLogger("pi_voice")


# textUtils.build_turn_suffix is the source of truth — pi_voice and
# openai_compat import from it via the xiaozhi-container
# bind mount at `core.utils.textUtils`. On the dev workstation the file
# lives at `custom-providers/textUtils.py` (the dash in the dir name
# makes it unimportable as a package), so we fall back to loading it
# by absolute path. Both code paths end up with the same module.
try:
    from core.utils.textUtils import (  # type: ignore
        ALLOWED_EMOJIS,
        FALLBACK_EMOJI,
        build_turn_suffix,
        filter_tts_stream,
    )
except ImportError:  # pragma: no cover — dev workstation fallback
    import importlib.util as _ilu
    from pathlib import Path as _Path

    _tu_path = _Path(__file__).resolve().parents[1] / "textUtils.py"
    _spec = _ilu.spec_from_file_location("dotty_textUtils", _tu_path)
    assert _spec is not None and _spec.loader is not None
    _tu = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_tu)
    ALLOWED_EMOJIS = _tu.ALLOWED_EMOJIS  # type: ignore[attr-defined]
    FALLBACK_EMOJI = _tu.FALLBACK_EMOJI  # type: ignore[attr-defined]
    build_turn_suffix = _tu.build_turn_suffix  # type: ignore[attr-defined]
    filter_tts_stream = _tu.filter_tts_stream  # type: ignore[attr-defined]


TAG = __name__
logger = setup_logging()


def _read_kid_mode() -> bool:
    """Read the shared runtime toggle, falling back to startup config."""
    state_file = Path(os.environ.get(
        "DOTTY_KID_MODE_STATE", "/var/lib/dotty-bridge/state/kid-mode",
    ))
    try:
        value = state_file.read_text().strip().lower()
        if value in ("true", "1", "yes"):
            return True
        if value in ("false", "0", "no"):
            return False
    except OSError:
        pass
    return os.environ.get("DOTTY_KID_MODE", "true").lower() in ("1", "true", "yes")


def _last_user_text(dialogue: list[dict]) -> str:
    """Find the most recent user-turn content. xiaozhi's dialogue is a
    list of {role, content} dicts in chronological order; the last user
    entry is the utterance we want pi to react to."""
    for msg in reversed(dialogue):
        if msg.get("role") == "user":
            return _normalise_user_content(msg.get("content"))
    return ""


def _last_user_turn(dialogue: list[dict]) -> tuple[str, str | None]:
    """Return model-visible text plus private correlation metadata."""
    for msg in reversed(dialogue):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        decoded = content
        if isinstance(content, str):
            stripped = content.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    decoded = json.loads(stripped)
                except json.JSONDecodeError:
                    decoded = content
        if isinstance(decoded, dict):
            text = decoded.get("content")
            turn_id = decoded.get("_dotty_turn_id")
            return (
                text if isinstance(text, str) else str(content or ""),
                str(turn_id)[:96] if turn_id else None,
            )
        return _normalise_user_content(content), None
    return "", None


def _normalise_user_content(content: object) -> str:
    """Extract xiaozhi's user text without leaking its JSON envelope to pi.

    Depending on where the dialogue was assembled, ``content`` may already be
    plain text, a ``{"content": "..."}`` mapping, or the JSON encoding of that
    mapping. Unknown JSON is kept verbatim: silently discarding or reshaping a
    genuine user utterance would be worse than passing it through.
    """
    if isinstance(content, dict):
        inner = content.get("content")
        return inner if isinstance(inner, str) else str(content or "")
    if not isinstance(content, str):
        return str(content or "")
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return content
        if isinstance(decoded, dict) and isinstance(decoded.get("content"), str):
            return decoded["content"]
    return content


_VOICE_TOOL_ROUTING = (
    "\n\nVOICE TOOL ROUTING: Decide whether to use a registered tool before "
    "composing the spoken reply. Use the matching tool when the user asks you "
    "to remember or recall, play a song, look or take a photo, or solve a "
    "question that needs careful reasoning. Call it first and use its result. "
    "Never claim an action succeeded unless its tool succeeded. The reply "
    "constraints below apply only to final spoken text, not to tool calls."
)


def _wrap_with_sandwich(user_text: str, kid_mode: bool) -> str:
    """Append the HARD CONSTRAINTS suffix to the user's text via the shared
    textUtils.build_turn_suffix contract — emoji-prefix
    rule, English-only, length caps, kid-mode topic filtering. Without
    this Dotty drifts into Chinese, multi-paragraph replies, and (in
    kid_mode) unsafe topics, since qwen3.5:4b's base behaviour doesn't
    encode any of those constraints."""
    return user_text + _VOICE_TOOL_ROUTING + build_turn_suffix(kid_mode)


def _enforce_leading_emoji(chunks: Iterator[str]) -> Iterator[str]:
    """Guarantee the firmware's leading-glyph face contract.

    Pi is prompted to start with an allowed emoji, but model compliance is not
    an output guarantee. Buffer only leading whitespace, then either pass an
    allowed emoji through or replace a missing/disallowed leading glyph with
    the neutral fallback before the model text.
    """
    leading: list[str] = []
    saw_content = False
    for chunk in chunks:
        if not chunk:
            continue
        if not saw_content:
            leading.append(chunk)
            so_far = "".join(leading).lstrip()
            if not so_far:
                continue
            saw_content = True
            if not any(so_far.startswith(emoji) for emoji in ALLOWED_EMOJIS):
                yield f"{FALLBACK_EMOJI} "
                # Do not leave a disallowed model emoji after the fallback:
                # `😐 ❤️ hello` still violates the exactly-one-face contract.
                # Consume the leading symbol plus emoji presentation/joiner
                # codepoints, while leaving ordinary punctuation and text.
                if so_far and unicodedata.category(so_far[0]) == "So":
                    end = 1
                    while end < len(so_far) and (
                        so_far[end] in ("\ufe0f", "\u200d")
                        or 0x1F3FB <= ord(so_far[end]) <= 0x1F3FF
                        or unicodedata.category(so_far[end]) == "So"
                    ):
                        end += 1
                    so_far = so_far[end:].lstrip()
            if so_far:
                yield so_far
            continue
        yield chunk

    if not saw_content:
        yield f"{FALLBACK_EMOJI} (no response)"


class LLMProvider(LLMProviderBase):
    """xiaozhi-server LLM provider backed by the dotty-pi container."""

    def __init__(self, config: dict, *, client: PiClient | None = None):
        self._container = config.get("container_name") or os.environ.get(
            "DOTTY_PI_CONTAINER", "dotty-pi",
        )
        # Initial value is logged for diagnostics. response() refreshes this
        # from the bridge/xiaozhi shared state file on every turn.
        self._kid_mode = _read_kid_mode()
        # `client` is injected by tests; production passes None to get
        # the env-configured default.
        self._client: PiClient = client if client is not None else make_default_pi_client()
        self._first_turn = True
        msg = f"PiVoiceLLM ready (container={self._container} kid_mode={self._kid_mode})"
        try:
            logger.bind(tag=TAG).info(msg)  # type: ignore[attr-defined]
        except AttributeError:
            logger.info(msg)

    # xiaozhi-server's voice loop calls this as a sync generator.
    # Each yielded string becomes a TTS chunk.
    def response(self, session_id, dialogue, **kwargs) -> Iterator[str]:
        self._kid_mode = _read_kid_mode()
        user_text, turn_id = _last_user_turn(dialogue)
        if not user_text:
            yield f"{FALLBACK_EMOJI} (empty turn)"
            return
        prompt = _wrap_with_sandwich(user_text, self._kid_mode)

        # Reset pi state between voice turns. First turn skips this —
        # the freshly-spawned process is already clean.
        if not self._first_turn:
            try:
                self._client.new_session()
            except PiClientError:
                logger.exception("PiVoiceLLM: new_session failed, continuing")
        self._first_turn = False

        model_started_at = time.monotonic()
        first_text_seen = False
        tool_started: dict[str, float] = {}

        def on_rpc_event(event: dict) -> None:
            nonlocal model_started_at, first_text_seen
            event_type = event.get("type")
            if event_type == "model_started":
                model_started_at = float(event.get("ts") or time.monotonic())
                _emit_activity_turn(
                    "model_started", turn_id, source="pi",
                    session_id=session_id,
                )
            elif event_type == "text_delta" and not first_text_seen:
                first_text_seen = True
                elapsed = (float(event.get("ts") or time.monotonic()) - model_started_at) * 1000.0
                _emit_activity_turn(
                    "first_text", turn_id, source="pi",
                    session_id=session_id, model_first_text_ms=max(0.0, elapsed),
                )
            elif event_type == "tool_started":
                tool_id = str(event.get("tool_call_id") or "")
                tool_started[tool_id] = float(event.get("ts") or time.monotonic())
                _emit_activity_turn(
                    "tool_started", turn_id, source="pi",
                    session_id=session_id, tool_call_id=tool_id,
                    tool_name=event.get("tool_name") or "tool",
                )
            elif event_type == "tool_finished":
                tool_id = str(event.get("tool_call_id") or "")
                ended = float(event.get("ts") or time.monotonic())
                duration = max(0.0, (ended - tool_started.pop(tool_id, ended)) * 1000.0)
                _emit_activity_turn(
                    "tool_finished", turn_id, source="pi",
                    session_id=session_id, tool_call_id=tool_id,
                    tool_name=event.get("tool_name") or "tool",
                    tool_ok=bool(event.get("tool_ok", True)),
                    tool_duration_ms=duration,
                )

        try:
            # #157: kid-mode blocked-content filter on TTS-bound output.
            # Full-turn buffered — the filter drains the pi RPC stream through
            # agent_end before making an atomic allow/replace decision.
            # Emoji enforcement precedes filtering, matching OpenAICompat: in
            # kid mode the filter still makes one atomic whole-turn decision;
            # outside it, chunks stream after the first meaningful one.
            spoken: list[str] = []
            for chunk in filter_tts_stream(
                _enforce_leading_emoji(
                    self._client.iter_turn_text(prompt, event_callback=on_rpc_event)
                ),
                self._kid_mode,
                on_hit=self._on_filter_hit,
            ):
                spoken.append(chunk)
                yield chunk
            response_text = "".join(spoken).strip()
            emoji = next(
                (candidate for candidate in ALLOWED_EMOJIS
                 if response_text.startswith(candidate)),
                "",
            )
            _emit_activity_turn(
                "response_ready", turn_id, source="pi",
                session_id=session_id, response_text=response_text,
                emoji_used=emoji,
            )
        except PiClientError as exc:
            logger.error("PiVoiceLLM turn failed: %s", exc)
            for line in self._client.recent_stderr()[-5:]:
                logger.error("  pi.stderr: %s", line)
            fallback = f"{FALLBACK_EMOJI} (brain offline — try again in a moment)"
            _emit_activity_turn(
                "failed", turn_id, source="pi", session_id=session_id,
                error=str(exc), response_text=fallback,
            )
            yield fallback

    def _on_filter_hit(self, tier: str, match) -> None:
        # Local logging only — the Prometheus counter / safety ring live in
        # the bridge container, which this provider can't reach.
        logger.warning(
            "PiVoiceLLM content-filter hit tier=%s pattern=%r — turn replaced",
            tier, match.group(),
        )

    def close(self) -> None:
        """xiaozhi may call this on shutdown — make sure pi cleans up."""
        self._client.close()
