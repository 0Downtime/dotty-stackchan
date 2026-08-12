import asyncio
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _ROOT / "custom-providers" / "openai_realtime" / "bridge.py"
_SPEC = importlib.util.spec_from_file_location("dotty_openai_realtime_under_test", _MODULE_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


class _BoundLogger:
    def __init__(self, records):
        self.records = records

    def debug(self, message):
        self.records.append(("debug", message))

    def info(self, message):
        self.records.append(("info", message))

    def warning(self, message):
        self.records.append(("warning", message))

    def error(self, message):
        self.records.append(("error", message))


class _Logger(_BoundLogger):
    def bind(self, **_kwargs):
        return _BoundLogger(self.records)


class _DeviceWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class _LocalAgent:
    def __init__(self):
        self.calls = []

    def response(self, session_id, dialogue):
        self.calls.append((session_id, dialogue))
        yield "😐 local "
        yield "result"


class _Connection:
    def __init__(self):
        self.session_id = "session-1"
        self.device_id = "device-private-id"
        self.headers = {"device-id": self.device_id}
        self.config = {"prompt": "You are Dotty."}
        self.websocket = _DeviceWebSocket()
        self.logger = _Logger([])
        self.llm = _LocalAgent()
        self.client_abort = False
        self.client_is_speaking = False
        self.client_listen_mode = "auto"
        self.need_bind = False
        self.close_after_chat = False
        self.last_activity_time = 12345.0
        self.sentence_id = "old-local-sentence"
        self.bind_completed_event = asyncio.Event()
        self.bind_completed_event.set()
        self.original_messages = []
        self.reset_count = 0
        self.clear_count = 0
        self.queue_clear_count = 0

    async def _route_message(self, message):
        self.original_messages.append(message)

    def reset_audio_states(self):
        self.reset_count += 1

    def clearSpeakStatus(self):
        self.client_is_speaking = False
        self.clear_count += 1

    def clear_queues(self):
        self.queue_clear_count += 1


class _Codec:
    def __init__(self):
        self.decoded = []
        self.encoded = []
        self.reset_count = 0

    def decode_input_opus(self, packet):
        self.decoded.append(packet)
        return b"pcm24"

    def encode_output_pcm(self, pcm):
        self.encoded.append(pcm)
        return [b"opus24"]

    def flush_output(self):
        return []

    def reset_output(self):
        self.reset_count += 1


class _RealtimeWebSocket:
    _STOP = object()

    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(json.dumps({"type": "session.created"}))
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is self._STOP:
            raise StopAsyncIteration
        return item

    async def send(self, message):
        payload = json.loads(message)
        self.sent.append(payload)
        if payload.get("type") == "session.update":
            self.incoming.put_nowait(json.dumps({"type": "session.updated"}))

    async def close(self):
        if not self.closed:
            self.closed = True
            self.incoming.put_nowait(self._STOP)


class _FailingClearWebSocket(_RealtimeWebSocket):
    async def send(self, message):
        if json.loads(message).get("type") == "input_audio_buffer.clear":
            raise ConnectionError("simulated upstream close")
        await super().send(message)


def _settings(**overrides):
    values = {
        "enabled": True,
        "api_key": "secret-test-key",
        "model": "gpt-realtime-2.1-mini",
        "voice": "marin",
        "name": "Dotty",
        "transcription_model": "gpt-live-transcribe",
        "reasoning_effort": "low",
        "connect_timeout_seconds": 1,
        "event_timeout_seconds": 1,
    }
    values.update(overrides)
    return _MODULE.RealtimeSettings(**values)


class TestOpenAIRealtime(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "DOTTY_KID_MODE": "false",
                "DOTTY_KID_MODE_STATE": "/private/tmp/dotty-test-kid-mode-missing",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_settings_do_not_reveal_api_key(self):
        settings = _settings(codex_broker_token="codex-broker-secret")
        self.assertNotIn("secret-test-key", repr(settings))
        self.assertNotIn("codex-broker-secret", repr(settings))
        self.assertIn("gpt-realtime-2.1-mini", settings.websocket_url)
        proxied = _settings(base_url="wss://example.test/realtime?tenant=dotty")
        self.assertIn("?tenant=dotty&model=", proxied.websocket_url)

    def test_web_search_uses_narrow_codex_function_tool(self):
        conn = _Connection()
        bridge = _MODULE.OpenAIRealtimeBridge(
            conn,
            _settings(
                codex_web_enabled=True,
                codex_broker_token="codex-broker-secret",
            ),
            codec_factory=_Codec,
        )
        tools = bridge._session_update()["session"]["tools"]
        self.assertEqual(len(tools), 2)
        web = tools[1]
        self.assertEqual(web["type"], "function")
        self.assertEqual(web["name"], "consult_codex_web")
        self.assertEqual(web["parameters"]["required"], ["query"])
        self.assertFalse(web["parameters"]["additionalProperties"])

    def test_web_search_stays_disabled_without_both_opt_in_and_key(self):
        conn = _Connection()
        for settings in (
            _settings(codex_web_enabled=False, codex_broker_token="present"),
            _settings(codex_web_enabled=True, codex_broker_token=""),
        ):
            bridge = _MODULE.OpenAIRealtimeBridge(
                conn,
                settings,
                codec_factory=_Codec,
            )
            self.assertEqual(
                [tool["name"] for tool in bridge._tool_definitions()],
                ["consult_dotty_local_agent"],
            )

    def test_transcription_can_be_disabled_without_disabling_voice(self):
        conn = _Connection()
        bridge = _MODULE.OpenAIRealtimeBridge(
            conn,
            _settings(transcription_model=""),
            codec_factory=_Codec,
        )
        session = bridge._session_update()["session"]
        self.assertNotIn("transcription", session["audio"]["input"])
        self.assertEqual(session["output_modalities"], ["audio"])

    def test_realtime_name_overrides_base_persona_identity(self):
        conn = _Connection()
        bridge = _MODULE.OpenAIRealtimeBridge(
            conn,
            _settings(name="ESP"),
            codec_factory=_Codec,
        )
        instructions = bridge._instructions()
        self.assertIn("current conversational name is ESP", instructions)
        self.assertIn("If asked your name, answer ESP", instructions)

    def test_kid_mode_fails_closed_to_original_route(self):
        async def run():
            conn = _Connection()
            calls = []

            async def connect(_url, _headers):
                calls.append(True)
                return _RealtimeWebSocket()

            bridge = _MODULE.attach_realtime_bridge(
                conn,
                _settings(),
                connect_factory=connect,
                codec_factory=_Codec,
            )
            self.assertIsNotNone(bridge)
            with patch.dict(os.environ, {"DOTTY_KID_MODE": "true"}, clear=False):
                await conn._route_message(json.dumps({"type": "listen", "state": "start"}))
            self.assertEqual(len(conn.original_messages), 1)
            self.assertEqual(calls, [])
            await bridge.close()

        asyncio.run(run())

    def test_missing_key_and_turn_start_failure_preserve_local_route(self):
        async def run():
            missing_key_conn = _Connection()
            missing_key_bridge = _MODULE.attach_realtime_bridge(
                missing_key_conn,
                _settings(api_key=""),
                connect_factory=lambda *_args: None,
                codec_factory=_Codec,
            )
            start = json.dumps({"type": "listen", "state": "start"})
            await missing_key_conn._route_message(start)
            self.assertEqual(missing_key_conn.original_messages, [start])
            await missing_key_bridge.close()

            failed_conn = _Connection()

            async def connect(_url, _headers):
                return _FailingClearWebSocket()

            failed_bridge = _MODULE.attach_realtime_bridge(
                failed_conn,
                _settings(),
                connect_factory=connect,
                codec_factory=_Codec,
            )
            await failed_conn._route_message(start)
            self.assertEqual(failed_conn.original_messages, [start])
            self.assertFalse(failed_bridge.connected)
            await failed_bridge.close()

        asyncio.run(run())

    def test_voice_turn_translates_opus_and_commits(self):
        async def run():
            conn = _Connection()
            conn.client_listen_mode = "manual"
            upstream = _RealtimeWebSocket()
            codec = _Codec()
            captured = {}

            async def connect(url, headers):
                captured["url"] = url
                captured["headers"] = headers
                return upstream

            bridge = _MODULE.attach_realtime_bridge(
                conn,
                _settings(),
                connect_factory=connect,
                codec_factory=lambda: codec,
            )
            self.assertEqual(conn.last_activity_time, 0.0)
            conn.close_after_chat = True
            conn.client_is_speaking = True
            await conn._route_message(json.dumps({"type": "listen", "state": "start"}))
            await conn._route_message(b"opus16")
            await conn._route_message(json.dumps({"type": "listen", "state": "stop"}))
            await conn._route_message(b"idle-opus")

            event_types = [event["type"] for event in upstream.sent]
            self.assertEqual(event_types[:2], ["session.update", "input_audio_buffer.clear"])
            self.assertIn("input_audio_buffer.append", event_types)
            self.assertEqual(event_types[-2:], ["input_audio_buffer.commit", "response.create"])
            append = next(e for e in upstream.sent if e["type"] == "input_audio_buffer.append")
            self.assertEqual(append["audio"], "cGNtMjQ=")
            self.assertEqual(codec.decoded, [b"opus16"])
            self.assertEqual(conn.original_messages, [])
            self.assertTrue(conn.client_abort)
            self.assertFalse(conn.close_after_chat)
            self.assertIsNone(conn.sentence_id)
            self.assertEqual(conn.queue_clear_count, 1)
            self.assertEqual(conn.client_listen_mode, "manual")
            self.assertNotIn("device-private-id", captured["headers"]["OpenAI-Safety-Identifier"])
            self.assertEqual(len(captured["headers"]["OpenAI-Safety-Identifier"]), 64)
            session = upstream.sent[0]["session"]
            self.assertEqual(session["model"], "gpt-realtime-2.1-mini")
            self.assertEqual(session["audio"]["input"]["turn_detection"], None)
            self.assertEqual(session["audio"]["output"]["format"]["rate"], 24000)
            self.assertEqual(session["tools"][0]["name"], "consult_dotty_local_agent")
            self.assertIn("do not speak or output an emoji", session["instructions"])
            await bridge.close()
            self.assertFalse(conn.client_abort)

        asyncio.run(run())

    def test_auto_mode_uses_server_vad_without_waiting_for_listen_stop(self):
        async def run():
            conn = _Connection()
            upstream = _RealtimeWebSocket()

            async def connect(_url, _headers):
                return upstream

            bridge = _MODULE.attach_realtime_bridge(
                conn,
                _settings(),
                connect_factory=connect,
                codec_factory=_Codec,
            )
            await conn._route_message(
                json.dumps({"type": "listen", "state": "start", "mode": "auto"})
            )
            await conn._route_message(b"opus16")

            turn_detection = upstream.sent[0]["session"]["audio"]["input"][
                "turn_detection"
            ]
            self.assertEqual(turn_detection["type"], "server_vad")
            self.assertTrue(turn_detection["create_response"])
            self.assertTrue(turn_detection["interrupt_response"])
            event_types = [event["type"] for event in upstream.sent]
            self.assertIn("input_audio_buffer.append", event_types)
            self.assertNotIn("input_audio_buffer.commit", event_types)
            self.assertNotIn("response.create", event_types)
            self.assertTrue(bridge.active_input)

            bridge._response_active = True
            append_count = event_types.count("input_audio_buffer.append")
            await conn._route_message(b"speaker-echo")
            current_types = [event["type"] for event in upstream.sent]
            self.assertEqual(
                current_types.count("input_audio_buffer.append"), append_count
            )
            self.assertEqual(conn.original_messages, [])
            await bridge.close()

        asyncio.run(run())

    def test_output_audio_is_encoded_and_sent_with_xiaozhi_controls(self):
        async def run():
            conn = _Connection()
            upstream = _RealtimeWebSocket()
            codec = _Codec()

            async def connect(_url, _headers):
                return upstream

            bridge = _MODULE.OpenAIRealtimeBridge(
                conn,
                _settings(),
                connect_factory=connect,
                codec_factory=lambda: codec,
            )
            self.assertTrue(await bridge.ensure_connected())
            await bridge._handle_server_event(
                {"type": "response.created", "response": {"id": "resp-1"}}
            )
            await bridge._handle_server_event(
                {
                    "type": "response.output_item.added",
                    "item": {"type": "message", "id": "item-1"},
                }
            )
            await bridge._handle_server_event(
                {"type": "response.output_audio_transcript.delta", "delta": "Hello"}
            )
            await bridge._handle_server_event(
                {"type": "response.output_audio.delta", "delta": "cGNt"}
            )
            await bridge._handle_server_event({"type": "response.output_audio.done"})
            await asyncio.sleep(0.09)

            json_frames = [json.loads(x) for x in conn.websocket.sent if isinstance(x, str)]
            self.assertTrue(any(x.get("type") == "llm" for x in json_frames))
            sentence = next(x for x in json_frames if x.get("state") == "sentence_start")
            self.assertEqual(sentence["text"], "Hello")
            self.assertIn(b"opus24", conn.websocket.sent)
            self.assertTrue(any(x.get("state") == "stop" for x in json_frames))
            self.assertEqual(codec.encoded, [b"pcm"])
            await bridge.close()

        asyncio.run(run())

    def test_interruption_cancels_and_truncates_played_audio(self):
        async def run():
            conn = _Connection()
            upstream = _RealtimeWebSocket()

            async def connect(_url, _headers):
                return upstream

            bridge = _MODULE.OpenAIRealtimeBridge(
                conn,
                _settings(),
                connect_factory=connect,
                codec_factory=_Codec,
            )
            self.assertTrue(await bridge.ensure_connected())
            bridge._response_active = True
            bridge._output_item_id = "item-1"
            bridge._played_ms = 180
            bridge._tts_started = True
            conn.client_is_speaking = True
            await bridge.interrupt()

            cancel = next(e for e in upstream.sent if e["type"] == "response.cancel")
            self.assertEqual(cancel, {"type": "response.cancel"})
            truncate = next(e for e in upstream.sent if e["type"] == "conversation.item.truncate")
            self.assertEqual(truncate["item_id"], "item-1")
            self.assertEqual(truncate["audio_end_ms"], 180)
            stop = json.loads(conn.websocket.sent[-1])
            self.assertEqual(stop["state"], "stop")
            await bridge.close()

        asyncio.run(run())

    def test_enabling_kid_mode_stops_already_queued_audio(self):
        async def run():
            conn = _Connection()
            upstream = _RealtimeWebSocket()

            async def connect(_url, _headers):
                return upstream

            bridge = _MODULE.OpenAIRealtimeBridge(
                conn,
                _settings(),
                connect_factory=connect,
                codec_factory=_Codec,
            )
            self.assertTrue(await bridge.ensure_connected())
            bridge._tts_started = True
            conn.client_is_speaking = True
            with patch.dict(os.environ, {"DOTTY_KID_MODE": "true"}, clear=False):
                await bridge._output_queue.put((bridge._playback_epoch, b"must-not-play"))
                await asyncio.sleep(0.02)
            self.assertNotIn(b"must-not-play", conn.websocket.sent)
            stop_frames = [
                json.loads(frame)
                for frame in conn.websocket.sent
                if isinstance(frame, str)
            ]
            self.assertTrue(any(frame.get("state") == "stop" for frame in stop_frames))
            await bridge.close()

        asyncio.run(run())

    def test_function_call_round_trips_through_local_agent(self):
        async def run():
            conn = _Connection()
            upstream = _RealtimeWebSocket()

            async def connect(_url, _headers):
                return upstream

            bridge = _MODULE.OpenAIRealtimeBridge(
                conn,
                _settings(),
                connect_factory=connect,
                codec_factory=_Codec,
            )
            self.assertTrue(await bridge.ensure_connected())
            await bridge._handle_server_event(
                {
                    "type": "response.done",
                    "response": {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "consult_dotty_local_agent",
                                "call_id": "call-1",
                                "arguments": json.dumps({"request": "What do you remember?"}),
                            }
                        ]
                    },
                }
            )
            tool_output = next(
                event
                for event in upstream.sent
                if event["type"] == "conversation.item.create"
            )
            decoded = json.loads(tool_output["item"]["output"])
            self.assertEqual(decoded["result"], "😐 local result")
            self.assertEqual(upstream.sent[-1], {"type": "response.create"})
            self.assertEqual(conn.llm.calls[0][0], "session-1")
            await bridge.close()

        asyncio.run(run())

    def test_codex_web_call_round_trips_through_private_broker_client(self):
        async def run():
            conn = _Connection()
            upstream = _RealtimeWebSocket()
            queries = []

            async def connect(_url, _headers):
                return upstream

            async def codex_web_client(query):
                queries.append(query)
                return "Current result. Sources: Example https://example.test"

            bridge = _MODULE.OpenAIRealtimeBridge(
                conn,
                _settings(
                    codex_web_enabled=True,
                    codex_broker_token="codex-broker-secret",
                ),
                connect_factory=connect,
                codec_factory=_Codec,
                codex_web_client=codex_web_client,
            )
            self.assertTrue(await bridge.ensure_connected())
            await bridge._handle_server_event(
                {
                    "type": "response.done",
                    "response": {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "consult_codex_web",
                                "call_id": "call-web-1",
                                "arguments": json.dumps(
                                    {"query": "What changed today?"}
                                ),
                            }
                        ]
                    },
                }
            )
            tool_output = next(
                event
                for event in upstream.sent
                if event["type"] == "conversation.item.create"
            )
            decoded = json.loads(tool_output["item"]["output"])
            self.assertIn("Current result", decoded["result"])
            self.assertEqual(queries, ["What changed today?"])
            self.assertEqual(upstream.sent[-1], {"type": "response.create"})
            self.assertIn(
                ("info", "Realtime Codex web research completed"),
                conn.logger.records,
            )
            await bridge.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
