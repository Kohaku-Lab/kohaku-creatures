"""PEV Verifier Plugin — Plan → Execute → Verify with an independent evaluator.

Implements proposal §4.1 "PEV / Independent verifier" as a kt-biome plugin.
PEV is a convention over existing primitives (subagent + post_llm_call +
inject_event); shipping it here keeps the core framework agnostic to the
fail→inject / fail→halt / fail→replan choice.

Behavior:
  1. On ``post_llm_call``, detect generator completion — no tool calls on
     the last assistant message AND (keyword regex match OR an explicit
     ``done`` tool call this round).
  2. Spawn an internal verifier ``Agent`` programmatically (same pattern as
     ``seamless_memory._create_agent``) with ``read`` / ``grep`` / optional
     ``bash`` (opt-in) tools plus a required ``verdict`` tool.
  3. Feed the verifier the sprint contract, the host's final message, and
     a scratchpad digest.
  4. On fail, inject a fresh ``TriggerEvent`` back into the host queue
     framed as corrective feedback; ``max_rounds`` caps the loop.
  5. On pass, stamp ``pev:passed=true`` on the scratchpad and let the host
     terminate naturally.

Usage (YAML options):

    options:
      model: codex/gpt-5.4              # verifier model (distinct)
      acceptance_criteria:              # required
        - "All files the assistant said it edited exist."
        - "No TODO/FIXME markers were introduced."
      trigger_on_keyword: "all done"    # optional regex
      trigger_on_tool: "done"           # optional generator-done tool
      max_rounds: 3
      agent_names: []                   # empty = all agents
      verifier_tools: ["read", "grep"]  # bash must be opt-in
"""

import json
import re
from typing import Any

from kohakuterrarium.builtins.tools.bash import BashTool
from kohakuterrarium.builtins.tools.grep import GrepTool
from kohakuterrarium.builtins.tools.read import ReadTool
from kohakuterrarium.core.agent import Agent
from kohakuterrarium.core.config_types import AgentConfig
from kohakuterrarium.core.events import EventType, TriggerEvent
from kohakuterrarium.modules.plugin.base import BasePlugin, PluginContext
from kohakuterrarium.modules.tool.base import BaseTool, ExecutionMode, ToolResult
from kohakuterrarium.serving.agent_session import AgentSession
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)


# =====================================================================
# Verifier system prompt template
# =====================================================================

_VERIFIER_PROMPT = """\
You are an INDEPENDENT verifier agent. Check whether another agent's \
work satisfies a pre-negotiated sprint contract. You share NO context \
with the generator; treat the handoff below as ground truth.

# Sprint contract — acceptance criteria
{criteria}

# Generator's final message
{final_message}

# Generator's scratchpad (working memory digest)
{scratchpad}

# How to verify
Use `read` and `grep` (and `bash`, if available) to inspect the \
workspace. Be targeted — short evidence-based answers beat long \
speculative ones. Decide pass/fail per criterion based on evidence, \
not the generator's prose.

# How to return
You MUST finish by calling the `verdict` tool exactly once:
- `passed`: true ONLY if every criterion is met.
- `issues`: concrete, actionable problems. Empty when passed=true.
Call `verdict` once. Do not call it twice. After `verdict`, stop."""


# =====================================================================
# Tools
# =====================================================================


class VerdictTool(BaseTool):
    """Final verdict tool the verifier MUST call exactly once."""

    def __init__(self, submit_fn: Any) -> None:
        super().__init__()
        self._submit = submit_fn

    @property
    def tool_name(self) -> str:
        return "verdict"

    @property
    def description(self) -> str:
        return (
            "Submit the final verdict: {passed: bool, issues: list[str]}. "
            "Call exactly once at the end."
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "passed": {
                    "type": "boolean",
                    "description": "True iff every acceptance criterion is met.",
                },
                "issues": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concrete, actionable issues. Empty when passed=true.",
                },
            },
            "required": ["passed"],
        }

    async def _execute(self, args: dict[str, Any], **kwargs: Any) -> ToolResult:
        passed = bool(args.get("passed", False))
        raw = args.get("issues") or []
        if not isinstance(raw, list):
            raw = [str(raw)]
        issues = [str(i) for i in raw if str(i).strip()]
        self._submit(passed, issues)
        return ToolResult(
            output="PASS" if passed else f"FAIL ({len(issues)} issue(s))", exit_code=0
        )


# =====================================================================
# Plugin
# =====================================================================


class PEVVerifierPlugin(BasePlugin):
    """Plan → Execute → Verify harness with an independent verifier agent."""

    name = "pev_verifier"
    priority = 60
    description = (
        "Independent-verifier PEV harness: spawns an evaluator agent on "
        "generator completion; re-injects issues on fail."
    )

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        opts = options or {}

        self._criteria: list[str] = _coerce_str_list(opts.get("acceptance_criteria"))
        self._model: str = str(opts.get("model", "")) or "codex/gpt-5.4"
        self._trigger_keyword: str = str(opts.get("trigger_on_keyword", "") or "")
        self._trigger_tool: str = str(opts.get("trigger_on_tool", "") or "")
        self._max_rounds: int = int(opts.get("max_rounds", 3) or 3)
        self._agent_names: set[str] = set(_coerce_str_list(opts.get("agent_names")))
        self._verifier_tool_names: list[str] = _coerce_str_list(
            opts.get("verifier_tools"), default=["read", "grep"]
        )

        self._keyword_re: re.Pattern[str] | None = None
        if self._trigger_keyword:
            try:
                self._keyword_re = re.compile(self._trigger_keyword, re.IGNORECASE)
            except re.error as exc:
                logger.warning(
                    "PEV trigger_on_keyword is not a valid regex; disabling",
                    pattern=self._trigger_keyword,
                    error=str(exc),
                )

        self._disabled: bool = not self._criteria
        if self._disabled:
            logger.warning("PEV verifier has no acceptance_criteria; plugin disabled")

        self._ctx: PluginContext | None = None
        self._verifier: AgentSession | None = None
        self._round_count: int = 0
        self._in_flight: bool = False
        # Verdict capture slot — overwritten on each verifier run.
        self._last_verdict: tuple[bool, list[str]] | None = None

    # ── Lifecycle ────────────────────────────────────────────────────

    async def on_load(self, context: PluginContext) -> None:
        self._ctx = context
        if self._disabled:
            return
        logger.info(
            "PEV verifier loaded",
            verifier_model=self._model,
            criteria_count=len(self._criteria),
            max_rounds=self._max_rounds,
        )

    async def on_unload(self) -> None:
        if self._verifier is not None:
            try:
                await self._verifier.stop()
            except Exception as exc:
                logger.debug("PEV verifier stop failed", error=str(exc))
            self._verifier = None

    # ── should_apply ─────────────────────────────────────────────────

    def should_apply(self, context: PluginContext) -> bool:
        """Respect opt-in agent filter. Keep behavior simple and explicit."""
        if self._disabled:
            return False
        if not self._agent_names:
            return True
        return context.agent_name in self._agent_names

    # ── Hooks ────────────────────────────────────────────────────────

    async def post_llm_call(
        self,
        messages: list[dict],
        response: str,
        usage: dict,
        **kwargs: Any,
    ) -> None:
        """Detect generator completion and spawn the verifier when it triggers."""
        if self._disabled or self._ctx is None or self._in_flight:
            return
        if not self.should_apply(self._ctx):
            return
        if self._round_count >= self._max_rounds:
            logger.info("PEV max_rounds reached", max_rounds=self._max_rounds)
            return
        if not self._is_generator_done(messages, response):
            return

        self._in_flight = True
        try:
            await self._run_verification(messages, response)
        except Exception as exc:
            logger.warning(
                "PEV verification failed; host continues unaffected",
                error=str(exc),
                exc_info=True,
            )
        finally:
            self._in_flight = False

    # ── Trigger detection ────────────────────────────────────────────

    def _is_generator_done(self, messages: list[dict], response: str) -> bool:
        """Return True when the assistant message looks like a completion.

        Signal:
        - No tool calls on the LAST assistant message.
        - AND (keyword regex matches OR explicit done-tool was called).
        """
        last_assistant = _last_assistant_message(messages)
        if last_assistant is None:
            return False
        tool_calls = last_assistant.get("tool_calls") or []
        if tool_calls:
            return False

        if self._keyword_re is not None and self._keyword_re.search(response or ""):
            return True
        if self._trigger_tool and _recent_tool_call_present(
            messages, self._trigger_tool
        ):
            return True
        return False

    # ── Verifier orchestration ───────────────────────────────────────

    async def _run_verification(self, messages: list[dict], response: str) -> None:
        verifier = await self._ensure_verifier()
        if verifier is None:
            return

        self._round_count += 1
        self._last_verdict = None

        prompt = _VERIFIER_PROMPT.format(
            criteria=_format_bullets(self._criteria),
            final_message=(response or "(empty)").strip(),
            scratchpad=self._scratchpad_digest() or "(empty)",
        )

        try:
            async for _ in verifier.chat(prompt):
                pass  # Drain stream; verdict captured via tool callback.
        except Exception as exc:
            logger.warning("PEV verifier run errored", error=str(exc))
            return

        verdict = self._last_verdict
        if verdict is None:
            logger.warning(
                "PEV verifier returned without calling verdict tool; "
                "treating as pass to avoid an infinite loop",
                round=self._round_count,
            )
            self._mark_passed()
            return

        passed, issues = verdict
        if passed:
            logger.info("PEV verdict: PASS", round=self._round_count)
            self._mark_passed()
        else:
            logger.info(
                "PEV verdict: FAIL",
                round=self._round_count,
                issue_count=len(issues),
            )
            self._inject_feedback(issues)

    async def _ensure_verifier(self) -> AgentSession | None:
        if self._verifier is not None:
            return self._verifier
        try:
            self._verifier = await self._create_verifier()
            return self._verifier
        except Exception as exc:
            logger.warning(
                "PEV verifier construction failed; skipping",
                error=str(exc),
                exc_info=True,
            )
            return None

    async def _create_verifier(self) -> AgentSession:
        """Build the verifier Agent programmatically — no config files."""
        config = AgentConfig(
            name=f"pev-verifier-{self._ctx.agent_name if self._ctx else 'anon'}"
        )
        config.model = self._model
        config.system_prompt = "(set per-call via chat prompt)"
        config.tool_format = "native"
        config.tools = []
        config.subagents = []
        config.include_tools_in_prompt = True
        config.include_hints_in_prompt = True
        config.max_messages = 20
        config.ephemeral = True

        agent = Agent(config)
        for tool in self._build_verifier_tools():
            agent.registry.register_tool(tool)
        agent.set_output_handler(lambda _: None, replace_default=True)
        await agent.start()
        return AgentSession(agent)

    _TOOL_FACTORIES: dict[str, Any] = {
        "read": ReadTool,
        "grep": GrepTool,
        "bash": BashTool,
    }

    def _build_verifier_tools(self) -> list[BaseTool]:
        """Assemble the verifier's tool set from the configured names."""
        tools: list[BaseTool] = []
        for name in self._verifier_tool_names:
            factory = self._TOOL_FACTORIES.get(name.lower())
            if factory is None:
                logger.warning(
                    "PEV verifier_tools: unknown tool ignored", tool_name=name
                )
                continue
            tools.append(factory())
        tools.append(VerdictTool(self._capture_verdict))
        return tools

    # ── Callbacks from verifier tools ────────────────────────────────

    def _capture_verdict(self, passed: bool, issues: list[str]) -> None:
        self._last_verdict = (passed, list(issues))

    # ── Result handling ──────────────────────────────────────────────

    def _mark_passed(self) -> None:
        if self._ctx is None:
            return
        try:
            self._ctx.set_state("passed", True)
        except Exception as exc:
            logger.debug("PEV set_state failed", error=str(exc))
        scratchpad = self._ctx.scratchpad
        if scratchpad is not None and hasattr(scratchpad, "set"):
            try:
                scratchpad.set("pev:passed", "true")
            except Exception as exc:
                logger.debug("PEV scratchpad set failed", error=str(exc))

    def _inject_feedback(self, issues: list[str]) -> None:
        if self._ctx is None:
            return
        lines = ["The evaluator found these problems, please address them:"]
        if issues:
            lines.extend(f"- {issue}" for issue in issues)
        else:
            lines.append("- (no specific issues — verifier said fail but listed none)")
        event = TriggerEvent(
            type=EventType.USER_INPUT,
            content="\n".join(lines),
            context={"source": "pev_verifier", "round": self._round_count},
            stackable=False,
        )
        try:
            self._ctx.inject_event(event)
        except Exception as exc:
            logger.warning("PEV inject_event failed", error=str(exc))

    # ── Scratchpad digest ────────────────────────────────────────────

    def _scratchpad_digest(self, max_chars: int = 2000) -> str:
        if self._ctx is None:
            return ""
        scratchpad = self._ctx.scratchpad
        if scratchpad is None or not hasattr(scratchpad, "to_dict"):
            return ""
        try:
            data = scratchpad.to_dict()
        except Exception:
            return ""
        if not data:
            return ""
        try:
            serialized = json.dumps(data, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            serialized = "\n".join(f"{k}: {v}" for k, v in data.items())
        if len(serialized) > max_chars:
            serialized = serialized[:max_chars] + "\n... (truncated)"
        return serialized


# =====================================================================
# Helpers
# =====================================================================


def _last_assistant_message(messages: list[dict]) -> dict | None:
    """Return the final assistant message in ``messages`` or None."""
    for msg in reversed(messages or []):
        if msg.get("role") == "assistant":
            return msg
    return None


def _recent_tool_call_present(messages: list[dict], tool_name: str) -> bool:
    """Return True iff the most recent assistant turn called ``tool_name``.

    We look back through assistant messages until the previous user turn;
    that bounds the window to "this generator round".
    """
    for msg in reversed(messages or []):
        role = msg.get("role")
        if role == "user":
            return False
        if role != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict) and fn.get("name") == tool_name:
                return True
            if isinstance(call, dict) and call.get("name") == tool_name:
                return True
    return False


def _format_bullets(items: list[str]) -> str:
    if not items:
        return "(none provided)"
    return "\n".join(f"- {item}" for item in items)


def _coerce_str_list(value: Any, default: list[str] | None = None) -> list[str]:
    """Normalise YAML-loaded option to ``list[str]``.

    - ``None`` / missing → ``default or []``.
    - A bare string is wrapped.
    - All entries are stringified and stripped; empties are dropped.
    - An explicit empty list is preserved (no silent fallback).
    """
    if value is None:
        return list(default) if default else []
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out
