"""Regression tests for wake-name corrections on the live ASR text path."""

import importlib.util
import json
import pathlib
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch


_ROOT = pathlib.Path(__file__).parent.parent


def _stub_module(name: str, **attrs) -> None:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


@contextmanager
def _container_import_stubs():
    """Install container-only imports for one module load, then restore them."""
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
    )
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in names}
    try:
        for package in ("core", "core.utils", "core.handle"):
            _stub_module(package)
        _stub_module("core.utils.util", audio_to_data=lambda *_args, **_kwargs: None)
        _stub_module("core.handle.abortHandle", handleAbortMessage=lambda *_args: None)
        _stub_module("core.handle.intentHandler", handle_user_intent=lambda *_args: None)
        _stub_module(
            "core.utils.output_counter",
            check_device_output_limit=lambda *_args: False,
        )
        _stub_module(
            "core.handle.sendAudioHandle",
            send_stt_message=lambda *_args: None,
            SentenceType=object,
        )
        _stub_module("core.utils.device_command", call_tool=lambda *_args, **_kwargs: None)
        yield
    finally:
        for name, module in previous.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


# receiveAudioHandle.py is a bind-mounted upstream override, so its normal
# imports only exist inside xiaozhi-server. Provide the smallest import surface
# needed to exercise its pure text helpers on the workstation without poisoning
# sys.modules for test modules collected later.
with _container_import_stubs():
    _spec = importlib.util.spec_from_file_location(
        "receive_audio_name_corrections_under_test", _ROOT / "receiveAudioHandle.py"
    )
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)


class TestAsrNameCorrections(unittest.TestCase):
    def test_observed_close_phonetic_variants_are_normalized(self):
        for heard in ("Dottie", "Duddy"):
            with self.subTest(heard=heard):
                corrected = _module._apply_asr_corrections(
                    f"Good night, {heard}."
                )
                self.assertEqual(corrected, "Good night, Dotty.")
                corrected = _module._apply_phrase_corrections(corrected)
                self.assertEqual(
                    _module._detect_state_phrase(corrected),
                    ("sleep", "Goodnight! 😴"),
                )

    def test_ambiguous_real_names_are_not_rewritten(self):
        for name in ("Donny", "Jody", "Jodi", "Claudia"):
            with self.subTest(name=name):
                text = f"Please say hello to {name}."
                self.assertEqual(_module._apply_asr_corrections(text), text)

    def test_alias_matching_is_word_bounded(self):
        text = "Duddybrook is a place."
        self.assertEqual(_module._apply_asr_corrections(text), text)


class TestActivityTurnPropagation(unittest.IsolatedAsyncioTestCase):
    def _connection(self, *, speaking=False):
        conn = types.SimpleNamespace()
        conn.logger = types.SimpleNamespace(
            bind=lambda **_kwargs: types.SimpleNamespace(
                info=lambda *_args, **_kwargs: None,
                warning=lambda *_args, **_kwargs: None,
                error=lambda *_args, **_kwargs: None,
            )
        )
        conn.need_bind = False
        conn.max_output_size = 0
        conn.headers = {"device-id": "device-1"}
        conn.device_id = "device-1"
        conn.session_id = "session-1"
        conn.vad_last_voice_time = 99_500.0
        conn.client_is_speaking = speaking
        conn.client_listen_mode = "auto"
        conn.current_state = "idle"
        conn.websocket = types.SimpleNamespace(send=AsyncMock())
        conn.executor = types.SimpleNamespace(submit=Mock())
        conn.chat = Mock()
        return conn

    async def test_corrected_transcript_gets_new_private_turn_envelope_and_asr_timing(self):
        conn = self._connection()
        events = []
        fixed_uuid = types.SimpleNamespace(hex="turn-new")
        with (
            patch.object(_module, "_sync_toggles_once", AsyncMock()),
            patch.object(_module, "handle_user_intent", AsyncMock(return_value=False)),
            patch.object(_module, "send_stt_message", AsyncMock()),
            patch.object(_module, "_emit_activity_turn", side_effect=lambda *a, **k: events.append((a, k))),
            patch.object(_module.time, "time", return_value=100.0),
            patch.object(_module.uuid, "uuid4", return_value=fixed_uuid),
        ):
            await _module.startToChat(conn, "Hello Dottie")

        phase, turn_id = events[0][0][:2]
        self.assertEqual((phase, turn_id), ("asr_completed", "turn-new"))
        self.assertEqual(events[0][1]["request_text"], "Hello Dotty")
        self.assertEqual(events[0][1]["asr_ms"], 500.0)
        prompt = conn.executor.submit.call_args.args[1]
        private = json.loads(prompt)
        self.assertEqual(private["content"], "Hello Dotty")
        self.assertEqual(private["_dotty_turn_id"], "turn-new")
        self.assertEqual(private["_dotty_request_text"], "Hello Dotty")
        self.assertEqual(
            list(conn._dotty_pending_activity_turns), [("turn-new", 100.0)],
        )

    async def test_barge_in_aborts_predecessor_and_starts_distinct_turn(self):
        conn = self._connection(speaking=True)
        conn._dotty_active_turn_id = "turn-old"
        events = []
        fixed_uuid = types.SimpleNamespace(hex="turn-successor")
        with (
            patch.object(_module, "_sync_toggles_once", AsyncMock()),
            patch.object(_module, "handle_user_intent", AsyncMock(return_value=False)),
            patch.object(_module, "handleAbortMessage", AsyncMock()),
            patch.object(_module, "send_stt_message", AsyncMock()),
            patch.object(_module, "_emit_activity_turn", side_effect=lambda *a, **k: events.append((a, k))),
            patch.object(_module.time, "time", return_value=100.0),
            patch.object(_module.uuid, "uuid4", return_value=fixed_uuid),
        ):
            await _module.startToChat(conn, "next question")

        phases = [(args[0], args[1]) for args, _kwargs in events]
        self.assertEqual(phases[:2], [
            ("asr_completed", "turn-successor"),
            ("aborted", "turn-old"),
        ])


if __name__ == "__main__":
    unittest.main()
