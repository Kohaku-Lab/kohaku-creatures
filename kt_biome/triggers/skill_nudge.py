"""Skill Nudge Trigger — periodic soft hints to persist procedural memory.

Every *interval_iterations* "processing cycles" (each `set_context` call
from the agent handlers) without a preceding ``skill_manage`` invocation,
fire a low-priority ``nudge`` TriggerEvent whose content is a short hint
asking the agent to consider saving a skill.

The trigger silences itself for *cooldown_iterations* cycles after every
fire, and also resets immediately when the paired
:mod:`kt_biome.tools.skill_manage` tool records activity — tracked via
:mod:`kt_biome.tools._skill_activity`.

Usage in config.yaml::

    triggers:
      - type: custom
        module: kt_biome.triggers.skill_nudge
        class_name: SkillNudgeTrigger
        options:
          interval_iterations: 8
          cooldown_iterations: 4
          enabled: true
"""

import asyncio
from typing import Any

from kohakuterrarium.core.events import TriggerEvent
from kohakuterrarium.modules.trigger.base import BaseTrigger
from kohakuterrarium.utils.logging import get_logger

from kt_biome.tools import _skill_activity

logger = get_logger(__name__)

_DEFAULT_NUDGE = (
    "You've just completed a non-trivial task. If this procedure is "
    "worth reusing, persist it as a SKILL.md bundle with the "
    "skill_manage tool (action='create'). Existing skills in "
    "~/.kohakuterrarium/skills/ or .kt/skills/ can be extended with "
    "action='patch'. Once saved, the skill is discoverable via "
    "##info <name>##, ##skill <name>##, or /<name> in a future session."
)

NUDGE_EVENT_TYPE = "skill_nudge"


class SkillNudgeTrigger(BaseTrigger):
    """Fire a periodic reminder to consider persisting a skill."""

    resumable = False
    universal = False

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        prompt: str | None = None,
        **extra: Any,
    ) -> None:
        # Allow both kwarg styles: options={} (kt-biome convention) or
        # inline kwargs (BaseTrigger resume style).
        opts: dict[str, Any] = dict(options or {})
        for key in (
            "interval_iterations",
            "cooldown_iterations",
            "enabled",
            "agent_name",
            "message",
        ):
            if key in extra:
                opts.setdefault(key, extra.pop(key))
        super().__init__(prompt=prompt, **extra)

        self._interval = max(1, int(opts.get("interval_iterations", 8)))
        self._cooldown = max(0, int(opts.get("cooldown_iterations", 4)))
        self._enabled = bool(opts.get("enabled", True))
        self._agent_name = str(opts.get("agent_name", "default"))
        self._message = str(opts.get("message", "") or _DEFAULT_NUDGE)

        # Iteration counters
        self._iterations = 0
        self._silence_until = 0  # iteration index at which firing resumes
        self._last_seen_skill_ts: float | None = _skill_activity.last_used_at(
            self._agent_name
        )

        # Async wake-up primitive
        self._ready: asyncio.Event | None = None

    # ── BaseTrigger hooks ─────────────────────────────────────────────

    def _ensure_events(self) -> None:
        if self._ready is None:
            self._ready = asyncio.Event()

    async def _on_start(self) -> None:
        self._ensure_events()
        if self._ready is not None:
            self._ready.clear()
        logger.debug(
            "skill_nudge trigger started",
            interval=self._interval,
            cooldown=self._cooldown,
            enabled=self._enabled,
        )

    async def _on_stop(self) -> None:
        self._ensure_events()
        if self._ready is not None:
            self._ready.set()
        logger.debug("skill_nudge trigger stopped")

    def _on_context_update(self, context: dict[str, Any]) -> None:
        """Count one iteration and decide whether to fire."""
        if not self._enabled:
            return

        self._iterations += 1

        # If skill_manage fired since we last looked, reset silently.
        current_ts = _skill_activity.last_used_at(self._agent_name)
        if current_ts is not None and current_ts != self._last_seen_skill_ts:
            self._last_seen_skill_ts = current_ts
            self._silence_until = self._iterations + self._cooldown
            logger.debug(
                "skill_nudge skipped (recent skill_manage)",
                iteration=self._iterations,
                silence_until=self._silence_until,
            )
            return

        if self._iterations < self._silence_until:
            return
        if self._iterations % self._interval != 0:
            return

        # Arm the fire — let wait_for_trigger produce the event.
        self._silence_until = self._iterations + self._cooldown
        self._ensure_events()
        if self._ready is not None:
            self._ready.set()
        logger.debug(
            "skill_nudge armed",
            iteration=self._iterations,
            next_silence_until=self._silence_until,
        )

    async def wait_for_trigger(self) -> TriggerEvent | None:
        """Block until the next nudge is armed (or the trigger stops)."""
        self._ensure_events()
        if not self._running:
            return None
        if self._ready is None:  # safety — _ensure_events always sets this
            return None
        await self._ready.wait()
        if not self._running:
            return None
        self._ready.clear()
        return TriggerEvent(
            type=NUDGE_EVENT_TYPE,
            content=self._message,
            context={
                "trigger": "skill_nudge",
                "iteration": self._iterations,
                "interval": self._interval,
            },
            prompt_override=self._message,
            stackable=True,
        )

    # ── Test hooks ────────────────────────────────────────────────────

    def _debug_state(self) -> dict[str, Any]:
        """Return internal counters — for tests only."""
        return {
            "iterations": self._iterations,
            "silence_until": self._silence_until,
            "enabled": self._enabled,
            "interval": self._interval,
            "cooldown": self._cooldown,
        }
