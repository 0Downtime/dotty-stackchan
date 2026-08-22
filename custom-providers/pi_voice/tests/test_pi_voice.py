"""Unit tests for PiVoiceLLM — the xiaozhi LLMProvider subclass.

Focus: prompt construction (last-user extraction + sandwich injection),
first-turn / nth-turn lifecycle, error fallback path. Live pi not
required — uses a fake PiClient.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
PROVIDER_DIR = os.path.dirname(HERE)
CUSTOM_PROVIDERS_DIR = os.path.dirname(PROVIDER_DIR)
sys.path.insert(0, PROVIDER_DIR)
sys.path.insert(0, CUSTOM_PROVIDERS_DIR)

import textUtils  # noqa: E402
# Import via the pi_voice package, not the top-level pi_client module —
# pi_voice catches pi_voice.pi_client.PiClientError, and `from pi_client
# import PiClientError` would give us a *different* class object even
# though the source is identical, so isinstance/except wouldn't match.
from pi_voice import (  # noqa: E402
    LLMProvider,
    PiClientError,
    _wrap_with_sandwich,
)
from pi_voice.pi_voice import (  # noqa: E402
    _last_user_text,
    _needs_live_device_status,
)


class FakeClient:
    """Stand-in for PiClient. Captures prompts; lets tests script the
    text-delta sequence + error injection."""

    def __init__(self):
        self.prompts: list[str] = []
        self.new_session_calls = 0
        self.scripted_chunks: list[list[str]] = []
        self.scripted_errors: list[BaseException | None] = []
        self.scripted_tool_events: list[list[dict]] = []
        self.closed = False

    def script_turn(
        self,
        chunks: list[str],
        error: BaseException | None = None,
        tool_events: list[dict] | None = None,
    ) -> None:
        self.scripted_chunks.append(chunks)
        self.scripted_errors.append(error)
        self.scripted_tool_events.append(tool_events or [])

    def new_session(self) -> None:
        self.new_session_calls += 1

    def iter_turn_text(
        self, prompt: str, on_tool_event=None, *, event_callback=None,
    ) -> Iterator[str]:
        self.prompts.append(prompt)
        chunks = self.scripted_chunks.pop(0) if self.scripted_chunks else []
        err = self.scripted_errors.pop(0) if self.scripted_errors else None
        events = self.scripted_tool_events.pop(0) if self.scripted_tool_events else []
        if err is not None:
            raise err
        if on_tool_event is not None:
            for event in events:
                on_tool_event(event)
        if event_callback:
            event_callback({"type": "model_started", "ts": 1.0})
        for c in chunks:
            if event_callback:
                event_callback({"type": "text_delta", "ts": 1.1})
            yield c

    def recent_stderr(self) -> list[str]:
        return []

    def close(self) -> None:
        self.closed = True


class TestSandwichInjection(unittest.TestCase):
    def test_suffix_appended_kid_mode_on(self):
        os.environ["DOTTY_KID_MODE"] = "true"
        client = FakeClient()
        client.script_turn(["😊 ", "Hi"])
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        list(provider.response("sess-1", [{"role": "user", "content": "Hello"}]))
        self.assertEqual(len(client.prompts), 1)
        self.assertTrue(client.prompts[0].startswith("Hello\n\nVOICE TOOL ROUTING:"))
        self.assertIn(textUtils.build_turn_suffix(True), client.prompts[0])
        marker = client.prompts[0].rsplit("[DOTTY_TURN_CONTEXT_V1]", 1)[1]
        context = json.loads(base64.urlsafe_b64decode(marker + "=" * (-len(marker) % 4)))
        self.assertEqual(context, {"session": "sess-1", "turn": 1, "utterance": "Hello"})
        # Sanity: the kid-mode-specific bullets must be in the suffix.
        self.assertIn("YOUNG CHILD", client.prompts[0])
        self.assertIn("SELF-HARM EXCEPTION", client.prompts[0])

    def test_suffix_appended_kid_mode_off(self):
        os.environ["DOTTY_KID_MODE"] = "false"
        client = FakeClient()
        client.script_turn(["😐 OK"])
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        list(provider.response("sess-1", [{"role": "user", "content": "Hi"}]))
        self.assertTrue(client.prompts[0].startswith("Hi\n\nVOICE TOOL ROUTING:"))
        self.assertIn(textUtils.build_turn_suffix(False), client.prompts[0])
        # Adult mode: still has emoji-prefix / English-only / no-Markdown
        # bullets, but NOT the kid-specific ones.
        self.assertIn("EXACTLY ONE emoji", client.prompts[0])
        self.assertNotIn("YOUNG CHILD", client.prompts[0])

    def test_wrap_helper_pure(self):
        # Tool routing must precede the final spoken-output constraints.
        wrapped = _wrap_with_sandwich("hi", True)
        self.assertTrue(wrapped.startswith("hi"))
        self.assertLess(wrapped.index("VOICE TOOL ROUTING"), wrapped.index("HARD CONSTRAINTS"))
        self.assertIn("not to tool calls", wrapped)
        self.assertIn("device_status for current volume", wrapped)
        self.assertIn("Never substitute file, shell, or coding tools", wrapped)

    def test_unambiguous_current_status_queries_are_detected(self):
        for text in (
            "Check your current speaker volume",
            "What's your battery level?",
            "How strong is the Wi-Fi signal right now?",
        ):
            self.assertTrue(_needs_live_device_status(text), text)
        for text in ("Tell me about batteries", "Can you hear me?", "Hello"):
            self.assertFalse(_needs_live_device_status(text), text)

    def test_verified_status_is_put_before_tool_and_spoken_constraints(self):
        wrapped = _wrap_with_sandwich(
            "Check your current speaker volume",
            True,
            live_device_status='{"audio_speaker":{"volume":70}}',
        )
        self.assertIn("runtime already called device_status", wrapped)
        self.assertIn('"volume":70', wrapped)
        self.assertLess(
            wrapped.index("VERIFIED LIVE DEVICE STATUS"),
            wrapped.index("VOICE TOOL ROUTING"),
        )

    def test_status_preflight_is_injected_for_a_live_status_turn(self):
        client = FakeClient()
        client.script_turn(["😊 Volume is 70 percent."])
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        with patch(
            "pi_voice.pi_voice._fetch_live_device_status",
            return_value='{"audio_speaker":{"volume":70}}',
        ) as fetch:
            output = list(provider.response(
                "s",
                [{"role": "user", "content": "Check your current volume"}],
            ))
        fetch.assert_called_once_with("Check your current volume")
        self.assertIn('"volume":70', client.prompts[0])
        self.assertEqual("".join(output), "😊 Volume is 70 percent.")

    def test_current_stackchan_identity_overrides_earlier_persona(self):
        wrapped = _wrap_with_sandwich("What is your name?", False)
        self.assertIn("Your name is StackChan", wrapped)
        self.assertIn("Refer to yourself only as StackChan", wrapped)
        self.assertIn("Do not call yourself Dotty", wrapped)

    def test_json_wrapped_user_content_is_unwrapped(self):
        dialogue = [{"role": "user", "content": '{"content": "remember purple"}'}]
        self.assertEqual(_last_user_text(dialogue), "remember purple")

    def test_private_turn_envelope_is_stripped_and_correlated(self):
        client = FakeClient()
        client.script_turn(["😊 hello"])
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        dialogue = [{
            "role": "user",
            "content": '{"content":"hello","_dotty_turn_id":"turn-7",'
                       '"_dotty_request_text":"hello"}',
        }]
        with patch("pi_voice.pi_voice._emit_activity_turn") as emit:
            self.assertEqual(list(provider.response("session-7", dialogue)), ["😊 hello"])
        self.assertTrue(client.prompts[0].startswith("hello\n\nVOICE TOOL ROUTING:"))
        self.assertNotIn("_dotty_turn_id", client.prompts[0])
        phases = [call.args[:2] for call in emit.call_args_list]
        self.assertIn(("model_started", "turn-7"), phases)
        self.assertIn(("first_text", "turn-7"), phases)
        self.assertIn(("response_ready", "turn-7"), phases)
        response_call = next(
            call for call in emit.call_args_list if call.args[0] == "response_ready"
        )
        self.assertEqual(response_call.kwargs["response_text"], "😊 hello")

    def test_mapping_wrapped_user_content_is_unwrapped(self):
        dialogue = [{"role": "user", "content": {"content": "think carefully"}}]
        self.assertEqual(_last_user_text(dialogue), "think carefully")

    def test_unknown_or_invalid_json_text_is_preserved(self):
        for content in ('{"question": "why"}', "{not json}"):
            self.assertEqual(
                _last_user_text([{"role": "user", "content": content}]),
                content,
            )

    def test_shared_state_file_refreshes_kid_mode_each_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "kid-mode"
            state_file.write_text("true")
            old_path = os.environ.get("DOTTY_KID_MODE_STATE")
            os.environ["DOTTY_KID_MODE_STATE"] = str(state_file)
            self.addCleanup(
                lambda: (
                    os.environ.__setitem__("DOTTY_KID_MODE_STATE", old_path)
                    if old_path is not None
                    else os.environ.pop("DOTTY_KID_MODE_STATE", None)
                )
            )

            client = FakeClient()
            client.script_turn(["😊 first"])
            client.script_turn(["😐 second"])
            provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
            list(provider.response("s", [{"role": "user", "content": "one"}]))

            state_file.write_text("false")
            list(provider.response("s", [{"role": "user", "content": "two"}]))

            self.assertIn("YOUNG CHILD", client.prompts[0])
            self.assertNotIn("YOUNG CHILD", client.prompts[1])

    def test_malformed_shared_state_falls_back_to_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "kid-mode"
            state_file.write_text("not-a-boolean")
            old_path = os.environ.get("DOTTY_KID_MODE_STATE")
            old_mode = os.environ.get("DOTTY_KID_MODE")
            os.environ["DOTTY_KID_MODE_STATE"] = str(state_file)
            os.environ["DOTTY_KID_MODE"] = "false"

            def restore_env() -> None:
                for name, value in (
                    ("DOTTY_KID_MODE_STATE", old_path),
                    ("DOTTY_KID_MODE", old_mode),
                ):
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

            self.addCleanup(restore_env)
            client = FakeClient()
            client.script_turn(["😐 adult mode"])
            provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
            list(provider.response("s", [{"role": "user", "content": "hello"}]))

            self.assertNotIn("YOUNG CHILD", client.prompts[0])


class TestEmptyTurn(unittest.TestCase):
    def test_no_user_message_short_circuits(self):
        os.environ["DOTTY_KID_MODE"] = "true"
        client = FakeClient()
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        out = list(provider.response("sess-1", [{"role": "system", "content": "..."}]))
        self.assertEqual(out, [f"{textUtils.FALLBACK_EMOJI} (empty turn)"])
        self.assertEqual(client.prompts, [], "PiClient must not be called for empty dialogue")


class TestNewSessionLifecycle(unittest.TestCase):
    def test_same_xiaozhi_session_preserves_pi_context(self):
        os.environ["DOTTY_KID_MODE"] = "true"
        client = FakeClient()
        client.script_turn(["ok"])
        client.script_turn(["ok"])
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        list(provider.response("s", [{"role": "user", "content": "a"}]))
        self.assertEqual(client.new_session_calls, 0, "no new_session on first turn")
        list(provider.response("s", [{"role": "user", "content": "b"}]))
        self.assertEqual(client.new_session_calls, 0, "same session keeps context")

    def test_new_xiaozhi_session_resets_pi_context(self):
        os.environ["DOTTY_KID_MODE"] = "true"
        client = FakeClient()
        client.script_turn(["ok"])
        client.script_turn(["ok"])
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        list(provider.response("session-a", [{"role": "user", "content": "a"}]))
        list(provider.response("session-b", [{"role": "user", "content": "b"}]))
        self.assertEqual(client.new_session_calls, 1)

    def test_disconnect_clears_only_matching_active_session(self):
        client = FakeClient()
        client.script_turn(["ok"])
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]

        list(provider.response("session-a", [{"role": "user", "content": "a"}]))
        provider.cancel_pending_confirmation("stale-session")
        self.assertEqual(client.new_session_calls, 0)
        provider.cancel_pending_confirmation("session-a")
        self.assertEqual(client.new_session_calls, 1)

        client.script_turn(["ok"])
        list(provider.response("session-a", [{"role": "user", "content": "b"}]))
        self.assertEqual(
            client.new_session_calls, 1,
            "first turn after disconnect starts from already-reset Pi state",
        )

    def test_missing_session_id_does_not_inherit_known_session(self):
        client = FakeClient()
        client.script_turn(["ok"])
        client.script_turn(["ok"])
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]

        list(provider.response("session-a", [{"role": "user", "content": "a"}]))
        list(provider.response(None, [{"role": "user", "content": "b"}]))

        self.assertEqual(client.new_session_calls, 1)

    def test_idless_session_resets_after_idle_timeout(self):
        client = FakeClient()
        client.script_turn(["ok"])
        client.script_turn(["ok"])
        provider = LLMProvider(
            {"session_idle_timeout_seconds": 120}, client=client,
        )  # type: ignore[arg-type]

        with patch("pi_voice.pi_voice.time.monotonic", side_effect=(100.0, 221.0)):
            list(provider.response(None, [{"role": "user", "content": "a"}]))
            list(provider.response(None, [{"role": "user", "content": "b"}]))

        self.assertEqual(client.new_session_calls, 1)

    def test_failed_boundary_reset_refuses_to_reuse_old_context(self):
        class FailingResetClient(FakeClient):
            def new_session(self) -> None:
                self.new_session_calls += 1
                raise PiClientError("reset failed")

        client = FailingResetClient()
        client.script_turn(["ok"])
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        list(provider.response(
            "session-a", [{"role": "user", "content": "private context"}],
        ))

        output = list(provider.response(
            "session-b", [{"role": "user", "content": "new conversation"}],
        ))

        self.assertEqual(client.new_session_calls, 1)
        self.assertEqual(len(client.prompts), 1, "new prompt must not reach old context")
        self.assertEqual(
            output,
            [f"{textUtils.FALLBACK_EMOJI} (brain offline — try again in a moment)"],
        )

    def test_concurrent_responses_are_serialized_through_agent_end(self):
        class OverlapDetectingClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.active = 0
                self.max_active = 0
                self.first_started = threading.Event()
                self.release_first = threading.Event()

            def iter_turn_text(
                self, prompt: str, on_tool_event=None, *, event_callback=None,
            ) -> Iterator[str]:
                self.prompts.append(prompt)
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                try:
                    if len(self.prompts) == 1:
                        self.first_started.set()
                        self.release_first.wait(timeout=2)
                    yield "😊 ok"
                finally:
                    self.active -= 1

        os.environ["DOTTY_KID_MODE"] = "false"
        client = OverlapDetectingClient()
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        outputs: list[list[str]] = []

        def run(text: str) -> None:
            outputs.append(list(provider.response(
                "s", [{"role": "user", "content": text}],
            )))

        first = threading.Thread(target=run, args=("first",))
        second = threading.Thread(target=run, args=("second",))
        first.start()
        self.assertTrue(client.first_started.wait(timeout=1))
        second.start()
        time.sleep(0.05)
        self.assertEqual(len(client.prompts), 1, "second turn must wait")
        client.release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(client.max_active, 1)
        self.assertEqual(len(outputs), 2)
        self.assertEqual(client.new_session_calls, 0)


class TestHomeAssistantConfirmationSpeech(unittest.TestCase):
    def test_blocked_first_write_uses_exact_non_model_confirmation_sentence(self):
        client = FakeClient()
        client.script_turn(
            ["😊 I already did it."],
            tool_events=[{
                "name": "home_assistant_HassTurnOn",
                "arguments": {"name": "Kitchen", "domain": "light"},
                "is_error": True,
                "result": {"content": [{
                    "type": "text",
                    "text": "DOTTY_CONFIRMATION_REQUIRED: Confirm on Kitchen",
                }]},
            }],
        )
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        output = list(provider.response(
            "session-a",
            [{"role": "user", "content": "Turn on Kitchen"}],
        ))
        self.assertEqual(output, [
            'Turn on Kitchen? Say "Confirm on Kitchen" within 15 seconds.',
        ])

    def test_mismatched_confirmation_never_claims_success(self):
        client = FakeClient()
        client.script_turn(
            ["😊 Done."],
            tool_events=[{
                "name": "home_assistant_HassTurnOn",
                "arguments": {"name": "Office"},
                "is_error": True,
                "result": {"content": [{
                    "type": "text",
                    "text": "DOTTY_CONFIRMATION_MISMATCH: action changed or expired",
                }]},
            }],
        )
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        output = list(provider.response(
            "session-a",
            [{"role": "user", "content": "Confirm on Kitchen"}],
        ))
        self.assertEqual(output, ["😐 Light action cancelled."])


class TestErrorFallback(unittest.TestCase):
    def test_client_error_yields_fallback(self):
        os.environ["DOTTY_KID_MODE"] = "true"
        client = FakeClient()
        client.script_turn([], error=PiClientError("pi crashed"))
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        out = list(provider.response("s", [{"role": "user", "content": "anything"}]))
        self.assertEqual(
            out,
            [f"{textUtils.FALLBACK_EMOJI} (brain offline — try again in a moment)"],
        )


class TestLeadingEmojiContract(unittest.TestCase):
    def _response(self, chunks: list[str], *, kid_mode: bool = False) -> list[str]:
        env = {
            "DOTTY_KID_MODE": "true" if kid_mode else "false",
            "DOTTY_KID_MODE_STATE": "/nonexistent/dotty-test-kid-mode",
        }
        with patch.dict(os.environ, env):
            client = FakeClient()
            client.script_turn(chunks)
            provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
            return list(provider.response("s", [{"role": "user", "content": "hello"}]))

    def test_missing_emoji_gets_fallback_before_first_text(self):
        out = self._response(["Hello", " there"])
        self.assertEqual(out[0], f"{textUtils.FALLBACK_EMOJI} ")
        self.assertEqual("".join(out), f"{textUtils.FALLBACK_EMOJI} Hello there")

    def test_leading_whitespace_never_precedes_emoji(self):
        out = self._response(["  ", "Hello"])
        self.assertTrue(out[0].startswith(textUtils.FALLBACK_EMOJI))
        self.assertEqual("".join(out), f"{textUtils.FALLBACK_EMOJI} Hello")

    def test_allowed_emoji_is_not_double_prefixed(self):
        out = self._response(["😊 Hello"])
        self.assertEqual(out, ["😊 Hello"])

    def test_disallowed_leading_emoji_is_replaced_not_retained(self):
        out = self._response(["❤️ Hello"])
        self.assertEqual("".join(out), f"{textUtils.FALLBACK_EMOJI} Hello")

    def test_full_catalog_single_codepoint_emoji_is_retained(self):
        out = self._response(["😂 Hello"])
        self.assertEqual("".join(out), "😂 Hello")

    def test_disallowed_single_codepoint_emoji_is_replaced(self):
        out = self._response(["🦄 Hello"])
        self.assertEqual("".join(out), f"{textUtils.FALLBACK_EMOJI} Hello")

    def test_new_canonical_face_emoji_is_preserved(self):
        out = self._response(["😂 Hello"])
        self.assertEqual("".join(out), "😂 Hello")

    def test_empty_model_stream_gets_emoji_fallback(self):
        out = self._response([])
        self.assertEqual(out, [f"{textUtils.FALLBACK_EMOJI} (no response)"])

    def test_kid_filter_still_replaces_the_complete_turn(self):
        out = self._response(["Hello ", "cocaine"], kid_mode=True)
        self.assertEqual(out, [textUtils.CONTENT_FILTER_REPLACEMENT])


if __name__ == "__main__":
    unittest.main()
