"""Regression tests for the server-driven dance state lifecycle."""

import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch


_ROOT = pathlib.Path(__file__).parent.parent


def _stub_module(name: str, **attrs) -> None:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


async def _execute_choreography(*_args, **_kwargs):
    return None


@contextmanager
def _container_import_stubs():
    names = (
        "core",
        "core.utils",
        "core.handle",
        "core.utils.util",
        "core.handle.abortHandle",
        "core.handle.intentHandler",
        "core.utils.output_counter",
        "core.handle.sendAudioHandle",
        "core.utils.device_command",
        "core.handle.dances",
    )
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in names}
    try:
        for package in ("core", "core.utils", "core.handle"):
            _stub_module(package)
        _stub_module("core.utils.util", audio_to_data=lambda *_args, **_kwargs: None)
        _stub_module("core.handle.abortHandle", handleAbortMessage=lambda *_args: None)
        _stub_module("core.handle.intentHandler", handle_user_intent=lambda *_args: None)
        _stub_module("core.utils.output_counter", check_device_output_limit=lambda *_args: False)
        _stub_module(
            "core.handle.sendAudioHandle",
            send_stt_message=lambda *_args: None,
            SentenceType=object,
        )
        _stub_module("core.utils.device_command", call_tool=lambda *_args, **_kwargs: None)
        _stub_module(
            "core.handle.dances",
            DANCE_REGISTRY={
                "test": {
                    "intro": "Test dance",
                    "choreography": "test",
                    "duration_ms": 0,
                }
            },
            AUDIO_LATENCY_OFFSET_MS=0,
            resolve_timeline=lambda _dance: [],
            execute_choreography=_execute_choreography,
        )
        yield
    finally:
        for name, module in previous.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


with _container_import_stubs():
    _spec = importlib.util.spec_from_file_location(
        "receive_audio_dance_state_under_test", _ROOT / "receiveAudioHandle.py"
    )
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)


class _Logger:
    def bind(self, **_kwargs):
        return self

    def info(self, *_args, **_kwargs):
        pass


class _WebSocket:
    async def send(self, _message):
        pass


class _Conn:
    logger = _Logger()
    websocket = _WebSocket()
    session_id = "session"
    sample_rate = 24000
    executor = types.SimpleNamespace(submit=lambda *_args, **_kwargs: None)

    def chat(self, _text):
        pass


class TestDanceStateLifecycle(unittest.TestCase):
    def test_barge_in_abort_does_not_poison_successor_chat(self):
        async def run():
            events = []
            conn = types.SimpleNamespace(
                logger=_Logger(),
                websocket=_WebSocket(),
                session_id="session",
                need_bind=False,
                max_output_size=0,
                client_is_speaking=True,
                client_listen_mode="auto",
                client_abort=False,
                current_state="idle",
            )

            async def abort_predecessor(target):
                events.append("abort")
                target.client_abort = True
                target.client_is_speaking = False

            async def inspect_intent(target, _text):
                events.append("intent")
                self.assertFalse(target.client_abort)
                return False

            def inspect_submit(target, text, **kwargs):
                events.append("submit")
                self.assertFalse(target.client_abort)
                self.assertEqual(text, "hello")
                self.assertIn("turn_id", kwargs)
                self.assertEqual(kwargs["request_text"], "hello")

            with (
                patch.object(_module, "_sync_toggles_once", new=AsyncMock()),
                patch.object(_module, "handleAbortMessage", side_effect=abort_predecessor),
                patch.object(_module, "handle_user_intent", side_effect=inspect_intent),
                patch.object(_module, "send_stt_message", new=AsyncMock()),
                patch.object(_module, "_submit_chat", side_effect=inspect_submit),
            ):
                await _module.startToChat(conn, "hello")

            self.assertEqual(events, ["abort", "intent", "submit"])
            self.assertFalse(conn.client_abort)

        asyncio.run(run())

    def test_led_helper_uses_firmware_schema_names(self):
        async def run():
            call = AsyncMock()
            with patch.object(_module, "_mcp_call_tool", new=call):
                await _module._send_led_color(_Conn(), 1, 2, 3)
            call.assert_awaited_once_with(
                unittest.mock.ANY,
                "self.robot.set_led_color",
                {"red": 1, "green": 2, "blue": 3},
            )

        asyncio.run(run())

    def test_dance_enters_firmware_state_then_returns_to_idle(self):
        async def run():
            states = []

            async def record_state(_conn, state):
                states.append(state)

            with (
                _container_import_stubs(),
                patch.object(_module, "_send_set_state", side_effect=record_state),
                patch.object(_module, "_send_led_color", new=AsyncMock()),
            ):
                await _module._handle_dance(_Conn(), "test")
                # Let the completed choreography task and its cleanup run.
                await asyncio.sleep(0.01)

            self.assertEqual(states, ["dance", "idle"])

        asyncio.run(run())

    def test_fast_dance_cleans_up_before_state_event_echo_arrives(self):
        async def run():
            conn = _Conn()
            conn._dotty_dance_generation = 1
            conn._dotty_desired_state = "dance"
            conn.current_state = "idle"  # production-shaped stale event echo
            states = []

            async def record_state(target_conn, state):
                target_conn._dotty_desired_state = state
                states.append(state)

            with (
                patch.object(_module, "_send_set_state", side_effect=record_state),
                patch.object(_module, "_send_led_color", new=AsyncMock()),
            ):
                await _module._run_owned_dance_choreography(
                    conn, 1, [], _execute_choreography,
                    audio_latency_offset_ms=0,
                )

            self.assertEqual(states, ["idle"])

        asyncio.run(run())

    def test_cancelled_dance_does_not_overwrite_new_security_or_sleep_state(self):
        async def run(successor):
            conn = _Conn()
            conn._dotty_dance_generation = 1
            conn._dotty_desired_state = "dance"
            conn.current_state = "dance"
            started = asyncio.Event()

            async def blocked_choreography(*_args, **_kwargs):
                started.set()
                await asyncio.Event().wait()

            set_state = AsyncMock()
            set_head = AsyncMock()
            set_led = AsyncMock()
            with (
                patch.object(_module, "_send_set_state", new=set_state),
                patch.object(_module, "_send_head_angles", new=set_head),
                patch.object(_module, "_send_led_color", new=set_led),
            ):
                task = asyncio.create_task(
                    _module._run_owned_dance_choreography(
                        conn, 1, [], blocked_choreography,
                        audio_latency_offset_ms=0,
                    )
                )
                await started.wait()
                # Mirrors _send_set_state's synchronous-before-await intent
                # update plus the firmware state_changed observation.
                conn._dotty_desired_state = successor
                conn.current_state = successor
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            set_state.assert_not_awaited()
            set_head.assert_not_awaited()
            set_led.assert_not_awaited()

        for successor in ("security", "sleep"):
            with self.subTest(successor=successor):
                asyncio.run(run(successor))

    def test_replaced_dance_owns_cleanup(self):
        async def run():
            conn = _Conn()
            conn._dotty_dance_generation = 2
            conn._dotty_desired_state = "dance"
            conn.current_state = "dance"
            set_state = AsyncMock()
            set_head = AsyncMock()
            set_led = AsyncMock()
            with (
                patch.object(_module, "_send_set_state", new=set_state),
                patch.object(_module, "_send_head_angles", new=set_head),
                patch.object(_module, "_send_led_color", new=set_led),
            ):
                await _module._run_owned_dance_choreography(
                    conn, 1, [], _execute_choreography,
                    audio_latency_offset_ms=0,
                )

            set_state.assert_not_awaited()
            set_head.assert_not_awaited()
            set_led.assert_not_awaited()

        asyncio.run(run())

    def test_call_site_finishes_cancel_cleanup_before_later_successor_state(self):
        async def run(successor):
            conn = _Conn()
            conn._dotty_dance_generation = 1
            conn._dotty_desired_state = "dance"
            conn.current_state = "dance"
            started = asyncio.Event()
            states = []

            async def blocked_choreography(*_args, **_kwargs):
                started.set()
                await asyncio.Event().wait()

            async def record_state(target_conn, state):
                target_conn._dotty_desired_state = state
                states.append(state)

            with (
                patch.object(_module, "_send_set_state", side_effect=record_state),
                patch.object(_module, "_send_head_angles", new=AsyncMock()),
                patch.object(_module, "_send_led_color", new=AsyncMock()),
            ):
                dance_task = asyncio.create_task(
                    _module._run_owned_dance_choreography(
                        conn, 1, [], blocked_choreography,
                        audio_latency_offset_ms=0,
                    )
                )
                await started.wait()
                # Actual startToChat ordering: cancel dance, await abort work,
                # then parse and dispatch the successor state phrase.
                await _module._cancel_active_dance(conn, dance_task)
                await asyncio.sleep(0)  # handleAbortMessage yields here
                await _module._send_set_state(conn, successor)

            self.assertEqual(states, ["idle", successor])

        for successor in ("security", "sleep", "dance"):
            with self.subTest(successor=successor):
                asyncio.run(run(successor))


if __name__ == "__main__":
    unittest.main()
