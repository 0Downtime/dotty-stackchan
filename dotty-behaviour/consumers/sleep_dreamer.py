"""Sleep dreamer — schedule N narrative dreams during the device's
sleep window.

On `state_changed → sleep`, schedules DREAM_COUNT_PER_NIGHT dreams at
evenly-spaced fractions of DREAM_WINDOW_SECONDS (default 3 dreams at
25/50/75% of an 8h window). Each dream calls NarrativeLLMClient with
a random literary seed, parses out an optional `SUMMARY:` trailing
line, and appends a record to the daily dreams NDJSON.

Any non-sleep state transition cancels pending dreams. The next
transition back to sleep schedules a fresh batch. Failures are
isolated per dream — one network blip doesn't kill the schedule.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid

from dispatch import NarrativeLLMClient
from logs import NdjsonWriter
from perception import PerceptionState

log = logging.getLogger("dotty-behaviour.consumers.sleep_dreamer")


DREAM_SYSTEM_PROMPT = (
    "You are StackChan, a small family robot, asleep. You are dreaming. "
    "Write rich, multi-paragraph robot dreams in first person, present "
    "tense — perception is strange, time bends, identity is fluid. "
    "Draw on the seed's atmosphere without retelling it. End with a "
    "half-thought, not a wrap. After the dream, on a new line starting "
    "with 'SUMMARY:', give a 1–2 sentence summary suitable for memory."
)
DREAM_USER_PROMPT_TEMPLATE = (
    "Tonight's seed: {seed}\n"
    "\n"
    "Write a dream of 4–7 paragraphs as StackChan would dream it.\n"
)


def split_dream_text(raw: str) -> tuple[str, str | None]:
    """Split a dream LLM reply into ``(full_text, summary)``. Looks for
    a final line starting with ``SUMMARY:`` (case-insensitive). When
    absent, returns the raw text and None — the dream still lands in
    the daily NDJSON; downstream FTS just doesn't get the short-form
    summary."""
    if not raw:
        return "", None
    text = raw.rstrip()
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if line.lower().startswith("summary:"):
            summary = line.split(":", 1)[1].strip()
            full_text = "\n".join(lines[:i]).rstrip()
            return full_text, (summary or None)
    return text, None


class SleepDreamer:
    def __init__(
        self,
        state: PerceptionState,
        narrative: NarrativeLLMClient,
        writer: NdjsonWriter,
        *,
        window_seconds: float,
        count_per_night: int,
        inspirations: tuple[str, ...],
    ) -> None:
        self._state = state
        self._narrative = narrative
        self._writer = writer
        self._window_seconds = window_seconds
        self._count_per_night = count_per_night
        self._inspirations = inspirations
        self._pending: dict[str, list[asyncio.Task]] = {}

    def _cancel_pending(self, device_id: str) -> None:
        tasks = self._pending.pop(device_id, [])
        for t in tasks:
            if not t.done():
                t.cancel()

    async def _fire_one(self, device_id: str, seed: str) -> None:
        dream_id = uuid.uuid4().hex
        user_prompt = DREAM_USER_PROMPT_TEMPLATE.format(seed=seed)
        log.info(
            "dream firing: device=%s seed=%s id=%s",
            device_id, seed, dream_id,
        )
        text = await self._narrative.chat(
            user_prompt,
            system_prompt=DREAM_SYSTEM_PROMPT,
            max_tokens=1200,
        )
        if not text:
            log.warning(
                "dream skipped (LLM returned no text): id=%s", dream_id
            )
            return
        full_text, summary = split_dream_text(text)
        self._writer.append(
            {
                "ts": self._writer.now_isoformat(),
                "type": "dream",
                "device": device_id,
                "dream_id": dream_id,
                "seed": seed,
                "summary": summary or "",
                "full_text": full_text,
            }
        )
        log.info(
            "dream saved: id=%s seed=%s chars=%d summary=%s",
            dream_id, seed, len(full_text), (summary or "")[:80],
        )

    def _schedule(self, device_id: str) -> None:
        self._cancel_pending(device_id)
        if self._count_per_night <= 0:
            return
        tasks: list[asyncio.Task] = []
        # Evenly-spaced fractions 1/(N+1) … N/(N+1). N=3 → 25/50/75%.
        for i in range(1, self._count_per_night + 1):
            delay = self._window_seconds * (i / (self._count_per_night + 1))
            seed = random.choice(self._inspirations)

            async def _delayed_fire(d: float, did: str, s: str) -> None:
                await asyncio.sleep(d)
                try:
                    await self._fire_one(did, s)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "dream fire crashed: device=%s seed=%s", did, s
                    )

            tasks.append(
                asyncio.create_task(_delayed_fire(delay, device_id, seed))
            )
        self._pending[device_id] = tasks
        log.info(
            "dreams scheduled: device=%s count=%d window=%.0fs",
            device_id, len(tasks), self._window_seconds,
        )

    async def run(self) -> None:
        log.info(
            "sleep dreamer started (window=%.0fs count=%d)",
            self._window_seconds, self._count_per_night,
        )
        q = self._state.subscribe()
        try:
            while True:
                event = await q.get()
                if event.name != "state_changed":
                    continue
                device_id = event.device_id
                if not device_id:
                    continue
                new_state = (
                    (event.data.get("state") or "").strip().lower()
                )
                if new_state == "sleep":
                    self._schedule(device_id)
                else:
                    # Any non-sleep transition cancels pending dreams.
                    # Next transition back to sleep schedules a fresh batch.
                    if device_id in self._pending:
                        log.info(
                            "dreams cancelled: device=%s reason=state=%s",
                            device_id, new_state,
                        )
                        self._cancel_pending(device_id)
        except asyncio.CancelledError:
            log.info("sleep dreamer cancelled")
            for did in list(self._pending.keys()):
                self._cancel_pending(did)
            raise
        except Exception:
            log.exception("sleep dreamer crashed")
        finally:
            self._state.unsubscribe(q)
