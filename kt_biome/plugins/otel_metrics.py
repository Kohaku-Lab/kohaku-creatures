"""OpenTelemetry Metrics Plugin — emit quantifiable agent metrics via OTLP.

Observes LLM calls, tool executions, sub-agent runs, compaction, and session
lifecycle via pre/post hooks.  Emits counters and histograms to an OTLP endpoint.

Requires: opentelemetry-api, opentelemetry-sdk, opentelemetry-exporter-otlp-proto-http
Install: pip install kt-biome[otel]

Usage:
    plugins:
      - name: otel_metrics
        module: kt_biome.plugins.otel_metrics
        class: OTelMetricsPlugin
        options:
          service_name: "kohaku-terrarium"
          endpoint: "http://localhost:4318/v1/metrics"
          export_interval: 30
"""

import time
from typing import Any

from kohakuterrarium.modules.plugin.base import BasePlugin, PluginContext
from kohakuterrarium.utils.logging import get_logger

logger = get_logger(__name__)

try:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    _otel_available: bool = True
except ImportError:
    _otel_available: bool = False

# ── Instrument definitions (module-level constants) ─────────────────

_COUNTER_DEFS: list[tuple[str, str]] = [
    ("kt.llm.calls", "LLM call count"),
    ("kt.llm.tokens.prompt", "Prompt tokens"),
    ("kt.llm.tokens.completion", "Completion tokens"),
    ("kt.llm.tokens.cached", "Cached tokens"),
    ("kt.tool.calls", "Tool call count"),
    ("kt.tool.dispatches", "Tool dispatch count"),
    ("kt.tool.errors", "Failed tool calls"),
    ("kt.subagent.runs", "Sub-agent run count"),
    ("kt.subagent.errors", "Failed sub-agent runs"),
    ("kt.compact.count", "Compaction count"),
    ("kt.events", "Event count"),
    ("kt.interrupts", "Interrupt count"),
]

_HISTOGRAM_DEFS: list[tuple[str, str, str]] = [
    ("kt.llm.duration", "LLM call latency", "ms"),
    ("kt.tool.duration", "Tool execution latency", "ms"),
    ("kt.subagent.duration", "Sub-agent run latency", "ms"),
    ("kt.subagent.turns", "Sub-agent turns", "1"),
    ("kt.compact.context_length", "Context length before compact", "1"),
    ("kt.compact.messages_removed", "Messages removed during compact", "1"),
    ("kt.agent.session.duration", "Agent session duration", "s"),
]


class OTelMetricsPlugin(BasePlugin):
    name = "otel_metrics"
    priority = 1  # First to observe

    def __init__(self, options: dict[str, Any] | None = None):
        opts = options or {}
        self._service_name: str = opts.get("service_name", "kohaku-terrarium")
        self._endpoint: str = opts.get("endpoint", "http://localhost:4318/v1/metrics")
        self._export_interval: int = int(opts.get("export_interval", 30))
        self._resource_attrs: dict[str, str] = opts.get("resource_attributes", {})
        self._agent_name: str = ""
        self._session_start: float = 0.0
        self._start_times: dict[int | str, float] = {}
        self._provider: Any | None = None
        self._meter: Any | None = None
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}

    # ── Centralised helpers ─────────────────────────────────────────

    def _inc(self, name: str, value: int | float, attrs: dict[str, str] | None = None) -> None:
        try:
            c = self._counters.get(name)
            if c is not None:
                c.add(value, attrs or {})
        except Exception:
            pass

    def _observe(self, name: str, value: int | float, attrs: dict[str, str] | None = None) -> None:
        try:
            h = self._histograms.get(name)
            if h is not None:
                h.record(value, attrs or {})
        except Exception:
            pass

    # ── Lifecycle ───────────────────────────────────────────────────

    async def on_load(self, context: PluginContext) -> None:
        self._agent_name = context.agent_name
        self._start_times = {}
        self._session_start = time.monotonic()
        if not _otel_available:
            logger.warning("opentelemetry packages not installed; plugin is no-op")
            return

        resource = Resource.create({"service.name": self._service_name, **self._resource_attrs})
        exporter = OTLPMetricExporter(endpoint=self._endpoint)
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=self._export_interval * 1000)
        self._provider = MeterProvider(resource=resource, metric_readers=[reader])
        self._meter = self._provider.get_meter("kohaku-terrarium")

        for name, desc in _COUNTER_DEFS:
            self._counters[name] = self._meter.create_counter(name, description=desc)
        for name, desc, unit in _HISTOGRAM_DEFS:
            self._histograms[name] = self._meter.create_histogram(name, description=desc, unit=unit)

        logger.info("OTel metrics initialised", endpoint=self._endpoint, service=self._service_name)

    async def on_unload(self) -> None:
        if self._provider is not None:
            try:
                self._provider.force_flush()
            except Exception:
                pass
            try:
                self._provider.shutdown()
            except Exception:
                pass
            self._provider = None
            self._meter = None

    async def on_agent_start(self) -> None:
        return None

    async def on_agent_stop(self) -> None:
        elapsed = time.monotonic() - self._session_start if self._session_start else 0
        self._observe("kt.agent.session.duration", elapsed, {"agent": self._agent_name})

    # ── LLM hooks ───────────────────────────────────────────────────

    async def pre_llm_call(self, messages, **kwargs):
        self._start_times[id(messages)] = time.monotonic()
        return None

    async def post_llm_call(self, messages, response, usage, **kwargs):
        model = kwargs.get("model", "")
        start = self._start_times.pop(id(messages), None)
        duration = (time.monotonic() - start) * 1000 if start is not None else 0
        u = usage or {}
        attrs = {"model": model}
        self._inc("kt.llm.calls", 1, attrs)
        self._inc("kt.llm.tokens.prompt", u.get("prompt_tokens", 0), attrs)
        self._inc("kt.llm.tokens.completion", u.get("completion_tokens", 0), attrs)
        self._inc("kt.llm.tokens.cached", u.get("cached_tokens", 0), attrs)
        self._observe("kt.llm.duration", duration, attrs)
        return None

    # ── Tool hooks ──────────────────────────────────────────────────

    async def pre_tool_dispatch(self, call, context):
        self._inc("kt.tool.dispatches", 1, {"tool_name": getattr(call, "name", "")})
        return None

    async def pre_tool_execute(self, args, **kwargs):
        self._start_times[kwargs.get("job_id", "")] = time.monotonic()
        return None

    async def post_tool_execute(self, result, **kwargs):
        tool_name = kwargs.get("tool_name", "")
        start = self._start_times.pop(kwargs.get("job_id", ""), None)
        duration = (time.monotonic() - start) * 1000 if start is not None else 0
        attrs = {"tool_name": tool_name}
        success = getattr(result, "success", True) if result else True
        self._inc("kt.tool.calls", 1, attrs)
        self._observe("kt.tool.duration", duration, attrs)
        if not success:
            self._inc("kt.tool.errors", 1, attrs)
        return None

    # ── Sub-agent hooks ─────────────────────────────────────────────

    async def pre_subagent_run(self, task, **kwargs):
        self._start_times[kwargs.get("job_id", "")] = time.monotonic()
        return None

    async def post_subagent_run(self, result, **kwargs):
        name = kwargs.get("name", "")
        start = self._start_times.pop(kwargs.get("job_id", ""), None)
        duration = (time.monotonic() - start) * 1000 if start is not None else 0
        success = getattr(result, "success", True)
        turns = getattr(result, "turns", 0)
        attrs = {"name": name}
        self._inc("kt.subagent.runs", 1, attrs)
        self._observe("kt.subagent.duration", duration, attrs)
        self._observe("kt.subagent.turns", turns, attrs)
        if not success:
            self._inc("kt.subagent.errors", 1, attrs)
        return None

    # ── Compact hooks ───────────────────────────────────────────────

    async def on_compact_start(self, context_length: int) -> None:
        self._inc("kt.compact.count", 1)
        self._observe("kt.compact.context_length", context_length)

    async def on_compact_end(self, summary: str, messages_removed: int) -> None:
        self._observe("kt.compact.messages_removed", messages_removed or 0)

    # ── Event / interrupt callbacks ─────────────────────────────────

    async def on_event(self, event=None) -> None:
        event_type = getattr(event, "type", "unknown") if event else "unknown"
        self._inc("kt.events", 1, {"event_type": event_type})

    async def on_interrupt(self) -> None:
        self._inc("kt.interrupts", 1)
