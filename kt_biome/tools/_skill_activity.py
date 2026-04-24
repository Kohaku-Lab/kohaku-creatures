"""Process-wide registry for skill_manage / skill_nudge coordination.

The ``SkillNudgeTrigger`` uses this to silence itself after the agent has
recently used ``skill_manage``. A process-wide dict is acceptable here —
both modules live in the same kt-biome distribution and the trigger and
tool are paired by configuration (not by framework wiring).

Keyed by an agent-supplied name (``ToolContext.agent_name`` for the
tool, falling back to ``"default"`` so standalone use still works).
"""

import threading
import time

_LOCK = threading.Lock()
# agent_name -> monotonic timestamp of last skill_manage call
_LAST_USED: dict[str, float] = {}


def mark_used(agent_name: str = "default") -> None:
    """Record that ``skill_manage`` was just invoked for *agent_name*."""
    with _LOCK:
        _LAST_USED[agent_name] = time.monotonic()


def last_used_at(agent_name: str = "default") -> float | None:
    """Return the monotonic timestamp of the last recorded call, or None."""
    with _LOCK:
        return _LAST_USED.get(agent_name)


def clear(agent_name: str | None = None) -> None:
    """Test helper — clear one entry, or all if *agent_name* is None."""
    with _LOCK:
        if agent_name is None:
            _LAST_USED.clear()
        else:
            _LAST_USED.pop(agent_name, None)
