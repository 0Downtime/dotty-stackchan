"""Dance reflector — on `dance_ended`, write a short LLM reflection
to the daily dances NDJSON.

Silent — no audio, no LED change, no state mutation. Each reflection
is independent; failures are logged and skipped (one network blip
doesn't compromise the bus).
"""

from __future__ import annotations

import asyncio
import logging

from dispatch import NarrativeLLMClient
from logs import NdjsonWriter
from perception import PerceptionState

log = logging.getLogger("dotty-behaviour.consumers.dance_reflector")


DANCE_SYSTEM_PROMPT = (
    "You are StackChan, a small family robot, reflecting privately on a "
    "dance you just finished. Write 2–3 sentences in first person, "
    "present tense, internal monologue (not spoken). Capture the joy, "
    "the rhythm, the silliness. Keep it under 300 characters."
)
DANCE_USER_PROMPT_TEMPLATE = "You just finished dancing to {dance}. How did it feel?"


class DanceReflector:
    def __init__(
        self,
        state: PerceptionState,
        narrative: NarrativeLLMClient,
        writer: NdjsonWriter,
    ) -> None:
        self._state = state
        self._narrative = narrative
        self._writer = writer
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro, *, name: str | None = None) -> None:
        t = asyncio.create_task(coro, name=name)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _fire(self, device_id: str, dance: str) -> None:
        user_prompt = DANCE_USER_PROMPT_TEMPLATE.format(dance=dance)
        text = await self._narrative.chat(
            user_prompt,
            system_prompt=DANCE_SYSTEM_PROMPT,
            max_tokens=200,
            temperature=0.85,
        )
        if not text:
            log.warning(
                "dance reflection skipped (no LLM text): device=%s dance=%s",
                device_id, dance,
            )
            return
        self._writer.append(
            {
                "ts": self._writer.now_isoformat(),
                "type": "dance",
                "device": device_id,
                "dance": dance,
                "reflection": text,
            }
        )
        log.info(
            "dance reflection saved: device=%s dance=%s chars=%d",
            device_id, dance, len(text),
        )

    async def run(self) -> None:
        log.info("dance reflector started")
        q = self._state.subscribe()
        try:
            while True:
                event = await q.get()
                if event.name != "dance_ended":
                    continue
                device_id = event.device_id
                if not device_id:
                    continue
                # `data.dance` may be empty if the firmware emitted a
                # bare dance_ended (older firmware); fall back to a
                # generic name.
                dance = (
                    (event.data.get("dance") or "the dance").strip()
                    or "the dance"
                )
                self._spawn(
                    self._fire(device_id, dance),
                    name="dance_reflector_fire",
                )
        except asyncio.CancelledError:
            log.info("dance reflector cancelled")
            for t in list(self._tasks):
                if not t.done():
                    t.cancel()
            raise
        except Exception:
            log.exception("dance reflector crashed")
        finally:
            self._state.unsubscribe(q)
