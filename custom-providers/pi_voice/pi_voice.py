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
  - Turns with the same xiaozhi `session_id` share pi's working state.
    When the xiaozhi session changes, we issue `new_session` without
    re-spawning the process.
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

import base64
import json
import os
import re
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Iterator

from .pi_client import PiClient, PiClientError, make_default_pi_client


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

_DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS = 120.0
_TURN_CONTEXT_MARKER = "[DOTTY_TURN_CONTEXT_V1]"


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


def _session_idle_timeout_seconds(config: dict) -> float:
    """Return the fallback boundary used when xiaozhi omits a session ID.

    Xiaozhi's voice connection defaults to a 120-second no-speech timeout.
    Matching that value keeps ID-less integrations useful without allowing
    their working context to survive indefinitely.
    """
    raw = config.get(
        "session_idle_timeout_seconds",
        os.environ.get(
            "DOTTY_PI_SESSION_IDLE_TIMEOUT_SECONDS",
            _DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS,
        ),
    )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS


def _last_user_text(dialogue: list[dict]) -> str:
    """Find the most recent user-turn content. xiaozhi's dialogue is a
    list of {role, content} dicts in chronological order; the last user
    entry is the utterance we want pi to react to."""
    for msg in reversed(dialogue):
        if msg.get("role") == "user":
            return _normalise_user_content(msg.get("content"))
    return ""


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
    "\n\nVOICE TOOL ROUTING: Before speaking, decide whether the answer depends "
    "on a registered Dotty tool. Questions can require tools too. Use "
    "device_status for current volume, battery, screen, or network status; "
    "memory_lookup or recall_person for facts learned earlier; remember or "
    "remember_person when explicitly asked to retain a durable fact; play_song "
    "for requested music; take_photo for the current room or camera view; and "
    "think_hard for precise math, technical, or factual reasoning; "
    "pilot_pilot_lookup for the explicit MCP pilot topic; "
    "home_assistant_GetLiveContext for selected live home sensor questions; "
    "and home_assistant_HassTurnOn or home_assistant_HassTurnOff only for one "
    "specifically named light. Call the "
    "matching tool first and base the spoken answer on its result. For greetings, "
    "opinions, simple conversation, or general knowledge you already know, answer "
    "without a tool. Never substitute file, shell, or coding tools. If no "
    "registered Dotty tool can perform the request, say so briefly; never pretend "
    "an action or lookup succeeded. The reply constraints below apply only to "
    "final spoken text, not to tool calls."
)


_DEVICE_STATUS_SUBJECT = re.compile(
    r"\b(volume|speaker|battery|charging|brightness|screen|theme|"
    r"network|wi-?fi|signal)\b",
    re.IGNORECASE,
)
_DEVICE_STATUS_CURRENT = re.compile(
    r"\b(current|currently|right now|status|level|check|what(?:'s| is)|"
    r"how (?:much|full|strong|bright|loud))\b",
    re.IGNORECASE,
)


def _needs_live_device_status(user_text: str) -> bool:
    """Recognize unambiguous questions about the robot's current state.

    A small local model is allowed to choose tools for ambiguous requests, but
    these high-confidence phrases are routed deterministically so it cannot
    claim to have checked the robot without actually doing so.
    """
    return bool(
        _DEVICE_STATUS_SUBJECT.search(user_text)
        and _DEVICE_STATUS_CURRENT.search(user_text)
    )


def _fetch_live_device_status(user_text: str) -> str:
    """Fetch and minimize current firmware status for a status-bound turn."""
    if not _needs_live_device_status(user_text):
        return ""
    token = os.environ.get("DOTTY_ADMIN_TOKEN", "").strip()
    port = os.environ.get("XIAOZHI_HTTP_PORT", "8003").strip() or "8003"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/xiaozhi/admin/device-status",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "X-Admin-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("PiVoiceLLM: live device-status preflight failed: %s", exc)
        return ""
    status = payload.get("status") if isinstance(payload, dict) else None
    if not isinstance(status, dict):
        return ""

    lowered = user_text.lower()
    selected: dict[str, object] = {}
    if "volume" in lowered or "speaker" in lowered:
        selected["audio_speaker"] = status.get("audio_speaker")
    if "battery" in lowered or "charging" in lowered:
        selected["battery"] = status.get("battery")
    if any(word in lowered for word in ("screen", "brightness", "theme")):
        selected["screen"] = status.get("screen")
    if any(word in lowered for word in ("network", "wifi", "wi-fi", "signal")):
        selected["network"] = status.get("network")
    selected = {key: value for key, value in selected.items() if value is not None}
    return json.dumps(selected or status, separators=(",", ":"))


def _wrap_with_sandwich(
    user_text: str,
    kid_mode: bool,
    live_device_status: str = "",
    turn_context: dict[str, object] | None = None,
) -> str:
    """Append the HARD CONSTRAINTS suffix to the user's text via the shared
    textUtils.build_turn_suffix contract — emoji-prefix
    rule, English-only, length caps, kid-mode topic filtering. Without
    this Dotty drifts into Chinese, multi-paragraph replies, and (in
    kid_mode) unsafe topics, since qwen3.5:4b's base behaviour doesn't
    encode any of those constraints."""
    verified = ""
    if live_device_status:
        verified = (
            "\n\nVERIFIED LIVE DEVICE STATUS: The runtime already called "
            f"device_status and received {live_device_status}. Do not call it "
            "again. State the exact relevant value in the spoken answer."
        )
    prompt = user_text + verified + _VOICE_TOOL_ROUTING + build_turn_suffix(kid_mode)
    if turn_context is not None:
        payload = json.dumps(turn_context, separators=(",", ":"), ensure_ascii=False)
        encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
        prompt += f"\n{_TURN_CONTEXT_MARKER}{encoded}"
    return prompt


def _tool_result_text(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _ha_confirmation_prompt(tool_event: dict) -> str | None:
    name = str(tool_event.get("name") or "")
    if not (name.lower().endswith("hassturnon") or name.lower().endswith("hassturnoff")):
        return None
    result_text = _tool_result_text(tool_event.get("result"))
    if "DOTTY_CONFIRMATION_REQUIRED:" not in result_text:
        return None
    arguments = tool_event.get("arguments")
    friendly = arguments.get("name") if isinstance(arguments, dict) else None
    if not isinstance(friendly, str) or not friendly.strip():
        return None
    friendly = " ".join(friendly.strip().split())
    action = "on" if name.lower().endswith("hassturnon") else "off"
    return (
        f'Turn {action} {friendly}? Say "Confirm {action} {friendly}" '
        "within 15 seconds."
    )


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
        # xiaozhi can invoke one provider from several priority/background
        # threads (for example a room-view greeter while a voice turn starts).
        # Pi RPC is a single ordered stream, so keep the complete
        # new_session -> prompt -> agent_end transaction exclusive.  Locking
        # only individual writes is insufficient: one caller can otherwise
        # consume another caller's response frames.
        self._turn_lock = threading.Lock()
        self._active_session_id: str | None = None
        self._last_turn_at: float | None = None
        self._has_pi_context = False
        self._turn_sequence = 0
        self._session_idle_timeout = _session_idle_timeout_seconds(config)
        msg = f"PiVoiceLLM ready (container={self._container} kid_mode={self._kid_mode})"
        try:
            logger.bind(tag=TAG).info(msg)  # type: ignore[attr-defined]
        except AttributeError:
            logger.info(msg)

    # xiaozhi-server's voice loop calls this as a sync generator.
    # Each yielded string becomes a TTS chunk.
    def response(self, session_id, dialogue, **kwargs) -> Iterator[str]:
        with self._turn_lock:
            yield from self._response_serialized(session_id, dialogue, **kwargs)

    def _response_serialized(self, session_id, dialogue, **kwargs) -> Iterator[str]:
        """Run one complete Pi RPC transaction while ``_turn_lock`` is held."""
        self._kid_mode = _read_kid_mode()
        user_text = _last_user_text(dialogue)
        if not user_text:
            yield f"{FALLBACK_EMOJI} (empty turn)"
            return
        # Keep ordinary follow-up turns in the same pi conversation. A new
        # xiaozhi audio-channel session is the authoritative boundary. If an
        # integration omits the ID, fall back to xiaozhi's no-speech timeout.
        normalized_session_id = str(session_id or "").strip() or None
        self._turn_sequence += 1
        live_device_status = _fetch_live_device_status(user_text)
        prompt = _wrap_with_sandwich(
            user_text,
            self._kid_mode,
            live_device_status=live_device_status,
            turn_context={
                "session": normalized_session_id or "",
                "turn": self._turn_sequence,
                "utterance": user_text,
            },
        )
        now = time.monotonic()
        reset_reason: str | None = None
        if self._has_pi_context:
            if normalized_session_id is not None:
                if normalized_session_id != self._active_session_id:
                    reset_reason = "xiaozhi session changed"
            elif self._active_session_id is not None:
                # Do not let an unscoped turn inherit a known session's context.
                reset_reason = "xiaozhi session id missing"
            elif (
                self._last_turn_at is not None
                and now - self._last_turn_at >= self._session_idle_timeout
            ):
                reset_reason = "id-less session idle timeout"

        if reset_reason is not None:
            try:
                self._client.new_session()
            except PiClientError:
                logger.exception(
                    f"PiVoiceLLM: new_session failed ({reset_reason}), refusing turn"
                )
                yield f"{FALLBACK_EMOJI} (brain offline — try again in a moment)"
                return
            else:
                logger.info(f"PiVoiceLLM: reset pi context ({reset_reason})")

        self._active_session_id = normalized_session_id
        self._last_turn_at = now
        self._has_pi_context = True

        try:
            # #157: kid-mode blocked-content filter on TTS-bound output.
            # Full-turn buffered — the filter drains the pi RPC stream through
            # agent_end before making an atomic allow/replace decision.
            # Emoji enforcement precedes filtering, matching OpenAICompat: in
            # kid mode the filter still makes one atomic whole-turn decision;
            # outside it, chunks stream after the first meaningful one.
            tool_events: list[dict] = []
            model_chunks = list(self._client.iter_turn_text(
                prompt,
                on_tool_event=tool_events.append,
            ))
            confirmation_prompt = next(
                (
                    prompt_text
                    for event in tool_events
                    if (prompt_text := _ha_confirmation_prompt(event)) is not None
                ),
                None,
            )
            if confirmation_prompt is not None:
                # The product contract requires this exact sentence. It is
                # generated from the already-blocked tool call, not by the model.
                yield confirmation_prompt
                return
            cancelled = any(
                "DOTTY_CONFIRMATION_" in _tool_result_text(event.get("result"))
                and event.get("is_error") is True
                for event in tool_events
            )
            selected_chunks = ["😐 Light action cancelled."] if cancelled else model_chunks
            for chunk in filter_tts_stream(
                _enforce_leading_emoji(iter(selected_chunks)),
                self._kid_mode,
                on_hit=self._on_filter_hit,
            ):
                yield chunk
        except PiClientError as exc:
            logger.error("PiVoiceLLM turn failed: %s", exc)
            for line in self._client.recent_stderr()[-5:]:
                logger.error("  pi.stderr: %s", line)
            yield f"{FALLBACK_EMOJI} (brain offline — try again in a moment)"

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

    def cancel_pending_confirmation(self, session_id: object) -> None:
        """Clear Pi state when the active Xiaozhi voice connection closes."""
        normalized = str(session_id or "").strip() or None
        if normalized is None:
            return
        with self._turn_lock:
            if normalized != self._active_session_id:
                return
            try:
                self._client.new_session()
            except PiClientError:
                logger.exception("PiVoiceLLM: disconnect session reset failed")
                # A dead process cannot retain an approval. Closing also makes
                # the next ordinary turn start a fresh Pi process.
                self._client.close()
            finally:
                self._active_session_id = None
                self._last_turn_at = None
                self._has_pi_context = False
