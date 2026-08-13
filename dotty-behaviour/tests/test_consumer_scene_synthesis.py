"""SceneSynthesisLoop + compose_scene_synthesis tests."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from consumers import SceneSynthesisLoop
from consumers.scene_synthesis import compose_scene_synthesis
from logs import NdjsonWriter
from perception import PerceptionEvent, PerceptionState

from ._fakes import let_consumer_settle


_UTC = ZoneInfo("UTC")
TRIGGER_EVENTS = frozenset({"face_recognized", "audio_captioned", "state_changed"})
TRIGGER_STATES = frozenset({"story_time", "security", "sleep"})


def test_compose_returns_none_when_both_caches_empty() -> None:
    state = PerceptionState()
    state.state["dev-1"] = {"face_present": True}
    out = compose_scene_synthesis(
        state, "dev-1",
        vision_ttl_sec=60.0, audio_ttl_sec=120.0,
        face_identity_ttl_sec=30.0, tz=_UTC,
    )
    assert out is None


def test_compose_with_vision_only() -> None:
    state = PerceptionState()
    state.state["dev-1"] = {"current_state": "idle"}
    state.vision_cache["dev-1"] = {
        "description": "a chair",
        "timestamp": perf_counter(),
        "wall_ts": time.time(),
    }
    out = compose_scene_synthesis(
        state, "dev-1",
        vision_ttl_sec=60.0, audio_ttl_sec=120.0,
        face_identity_ttl_sec=30.0, tz=_UTC,
    )
    assert out is not None
    assert "StackChan sees a chair" in out["text"]
    assert "State: idle" in out["text"]
    assert out["has_vision"] is True
    assert out["has_audio_caption"] is False


def test_compose_includes_face_id_when_fresh() -> None:
    state = PerceptionState()
    state.state["dev-1"] = {
        "current_state": "idle",
        "last_face_id": "brett",
        "last_face_recognized_t": time.time() - 1.0,
    }
    state.vision_cache["dev-1"] = {
        "description": "a chair",
        "timestamp": perf_counter(),
        "wall_ts": time.time(),
    }
    out = compose_scene_synthesis(
        state, "dev-1",
        vision_ttl_sec=60.0, audio_ttl_sec=120.0,
        face_identity_ttl_sec=30.0, tz=_UTC,
    )
    assert out is not None
    assert "brett is in the room" in out["text"]


def test_compose_drops_stale_vision_and_audio() -> None:
    state = PerceptionState()
    state.state["dev-1"] = {"current_state": "idle"}
    # Timestamps from perf_counter aren't comparable across processes,
    # so use 0 → arbitrarily old by now_perf default.
    state.vision_cache["dev-1"] = {
        "description": "a chair",
        "timestamp": 0.0,
        "wall_ts": 0.0,
    }
    state.audio_cache["dev-1"] = {
        "description": "music",
        "timestamp": 0.0,
        "wall_ts": 0.0,
    }
    out = compose_scene_synthesis(
        state, "dev-1",
        vision_ttl_sec=0.1, audio_ttl_sec=0.1,
        face_identity_ttl_sec=30.0, tz=_UTC,
        now_perf=10_000.0,
    )
    assert out is None


def _make_loop(td: Path, state: PerceptionState, *, interval=0.05,
               min_gap=0.0) -> SceneSynthesisLoop:
    return SceneSynthesisLoop(
        state,
        NdjsonWriter(td, "scene-synthesis", _UTC),
        interval_sec=interval,
        min_gap_sec=min_gap,
        trigger_events=TRIGGER_EVENTS,
        trigger_states=TRIGGER_STATES,
        vision_ttl_sec=60.0,
        audio_ttl_sec=120.0,
        face_identity_ttl_sec=30.0,
        tz=_UTC,
    )


def test_loop_emits_on_tick_when_caches_have_content() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = PerceptionState()
            state.state["dev-1"] = {"current_state": "idle"}
            state.vision_cache["dev-1"] = {
                "description": "a chair",
                "timestamp": perf_counter(),
                "wall_ts": time.time(),
            }
            loop = _make_loop(tdp, state, interval=0.05)
            task = asyncio.create_task(loop.run())
            try:
                # Wait for the first tick + write
                await asyncio.sleep(0.15)
                files = list(tdp.glob("scene-synthesis-*.ndjson"))
                assert len(files) == 1
                record = json.loads(
                    files[0].read_text(encoding="utf-8").splitlines()[0]
                )
                assert record["type"] == "scene_synthesis"
                assert record["device"] == "dev-1"
                assert "ts_wall" not in record  # stripped before write
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())


def test_loop_emits_on_trigger_event() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = PerceptionState()
            state.state["dev-1"] = {"current_state": "idle"}
            state.audio_cache["dev-1"] = {
                "description": "soft music",
                "timestamp": perf_counter(),
                "wall_ts": time.time(),
            }
            # Long interval so tick isn't what fires this — must be the event
            loop = _make_loop(tdp, state, interval=10.0, min_gap=0.0)
            task = asyncio.create_task(loop.run())
            try:
                await let_consumer_settle()
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="audio_captioned",
                        data={"preview": "soft music"},
                        ts=time.time(),
                    )
                )
                await let_consumer_settle()
                await asyncio.sleep(0.05)
                files = list(tdp.glob("scene-synthesis-*.ndjson"))
                assert len(files) == 1
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())


def test_loop_min_gap_suppresses_burst() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = PerceptionState()
            state.state["dev-1"] = {"current_state": "idle"}
            state.audio_cache["dev-1"] = {
                "description": "soft music",
                "timestamp": perf_counter(),
                "wall_ts": time.time(),
            }
            # Long min_gap → only the first burst event emits
            loop = _make_loop(tdp, state, interval=10.0, min_gap=60.0)
            task = asyncio.create_task(loop.run())
            try:
                await let_consumer_settle()
                for _ in range(3):
                    state.broadcast(
                        PerceptionEvent(
                            device_id="dev-1",
                            name="audio_captioned",
                            data={},
                            ts=time.time(),
                        )
                    )
                await let_consumer_settle()
                await asyncio.sleep(0.05)
                files = list(tdp.glob("scene-synthesis-*.ndjson"))
                assert len(files) == 1
                lines = files[0].read_text(encoding="utf-8").splitlines()
                assert len(lines) == 1  # only first event emitted
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())


def test_loop_emits_scene_synthesised_event() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = PerceptionState()
            state.state["dev-1"] = {"current_state": "idle"}
            state.audio_cache["dev-1"] = {
                "description": "x",
                "timestamp": perf_counter(),
                "wall_ts": time.time(),
            }
            loop = _make_loop(tdp, state, interval=10.0, min_gap=0.0)
            task = asyncio.create_task(loop.run())
            try:
                await let_consumer_settle()
                observer = state.subscribe()
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="audio_captioned",
                        data={},
                        ts=time.time(),
                    )
                )
                seen = []
                # Drain a few events looking for scene_synthesised
                for _ in range(5):
                    try:
                        ev = await asyncio.wait_for(observer.get(), timeout=0.5)
                        seen.append(ev.name)
                        if ev.name == "scene_synthesised":
                            break
                    except asyncio.TimeoutError:
                        break
                state.unsubscribe(observer)
                assert "scene_synthesised" in seen
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
