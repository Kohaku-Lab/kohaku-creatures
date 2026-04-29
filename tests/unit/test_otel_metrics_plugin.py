"""Tests for kt-biome's OTelMetricsPlugin.

All OpenTelemetry imports are mocked — no real OTEL packages required.
Uses a controllable ``Clock`` stub for deterministic timing behaviour.
"""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ── Bootstrap: inject mock kohakuterrarium so the plugin can import ──

_mock_kt = ModuleType("kohakuterrarium")
_mock_kt_modules: dict[str, ModuleType] = {}


def _ensure_mod(dotted: str) -> ModuleType:
    parts = dotted.split(".")
    for i in range(len(parts)):
        partial = ".".join(parts[: i + 1])
        if partial not in sys.modules:
            m = ModuleType(partial)
            sys.modules[partial] = m
        if partial not in _mock_kt_modules:
            _mock_kt_modules[partial] = sys.modules[partial]
    return sys.modules[dotted]


# BasePlugin and PluginContext — minimal stubs
class _BasePlugin:
    name: str = ""
    priority: int = 0

    def __init__(self, options=None):
        pass


base_mod = _ensure_mod("kohakuterrarium.modules.plugin.base")
base_mod.BasePlugin = _BasePlugin
base_mod.PluginContext = SimpleNamespace

plugin_pkg = _ensure_mod("kohakuterrarium.modules.plugin")
plugin_pkg.BasePlugin = _BasePlugin

# kohakuterrarium.utils.logging
logging_mod = _ensure_mod("kohakuterrarium.utils.logging")
logging_mod.get_logger = lambda *a, **kw: MagicMock()

# Ensure top-level kohakuterrarium and all needed subpackages
_ensure_mod("kohakuterrarium.modules")
_ensure_mod("kohakuterrarium.utils")
_ensure_mod("kohakuterrarium.session")
_ensure_mod("kohakuterrarium")

# Suppress the real opentelemetry so the plugin's try/except sets
# _otel_available = False cleanly (it already is False in this env).
for _blocked in [
    "opentelemetry",
    "opentelemetry.exporter",
    "opentelemetry.exporter.otlp",
    "opentelemetry.exporter.otlp.proto",
    "opentelemetry.exporter.otlp.proto.http",
    "opentelemetry.exporter.otlp.proto.http.metric_exporter",
    "opentelemetry.sdk",
    "opentelemetry.sdk.metrics",
    "opentelemetry.sdk.metrics.export",
    "opentelemetry.sdk.resources",
]:
    if _blocked not in sys.modules:
        sys.modules[_blocked] = ModuleType(_blocked)

# NOW import the module under test
from kt_biome.plugins import otel_metrics as mod
from kt_biome.plugins.otel_metrics import OTelMetricsPlugin


# ── Helpers ──────────────────────────────────────────────────────────


class Clock:
    """Mutable monotonic-clock stub."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _ctx(agent_name: str = "test-agent"):
    """Minimal PluginContext stand-in."""
    return SimpleNamespace(agent_name=agent_name)


def _make_plugin(options: dict | None = None) -> OTelMetricsPlugin:
    """Create plugin with mocked OTEL instruments pre-populated."""
    plugin = OTelMetricsPlugin(options)
    for name, _ in mod._COUNTER_DEFS:
        plugin._counters[name] = MagicMock()
    for name, _, _ in mod._HISTOGRAM_DEFS:
        plugin._histograms[name] = MagicMock()
    return plugin


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graceful_no_otel() -> None:
    """1. Plugin is a no-op when OTEL packages are not available."""
    plugin = OTelMetricsPlugin()
    await plugin.on_load(_ctx("lonely"))
    assert plugin._agent_name == "lonely"

    msgs = ["hello"]
    await plugin.post_llm_call(msgs, None, {"prompt_tokens": 100}, model="gpt-5.4")

    assert plugin._counters == {}
    assert plugin._histograms == {}


@pytest.mark.asyncio
async def test_on_load_creates_instruments() -> None:
    """2. on_load creates all 12 counters and 7 histograms via the meter."""
    mock_meter = MagicMock()
    mock_provider = MagicMock()
    mock_provider.get_meter.return_value = mock_meter

    mock_resource_cls = MagicMock()
    mock_exporter_cls = MagicMock()
    mock_reader_cls = MagicMock()
    mock_meter_provider_cls = MagicMock(return_value=mock_provider)

    p_avail = patch("kt_biome.plugins.otel_metrics._otel_available", True)
    p_resource = patch("kt_biome.plugins.otel_metrics.Resource", mock_resource_cls, create=True)
    p_exporter = patch("kt_biome.plugins.otel_metrics.OTLPMetricExporter", mock_exporter_cls, create=True)
    p_reader = patch("kt_biome.plugins.otel_metrics.PeriodicExportingMetricReader", mock_reader_cls, create=True)
    p_provider = patch("kt_biome.plugins.otel_metrics.MeterProvider", mock_meter_provider_cls, create=True)

    with p_avail, p_resource, p_exporter, p_reader, p_provider:
        plugin = OTelMetricsPlugin({"endpoint": "http://otel:4318/v1/metrics"})
        await plugin.on_load(_ctx())

    mock_meter.create_counter.assert_called()
    mock_meter.create_histogram.assert_called()
    assert mock_meter.create_counter.call_count == len(mod._COUNTER_DEFS)
    assert mock_meter.create_histogram.call_count == len(mod._HISTOGRAM_DEFS)


@pytest.mark.asyncio
async def test_on_unload_shuts_down_provider() -> None:
    """3. on_unload force-flushes and shuts down the provider."""
    plugin = _make_plugin()
    mock_provider = MagicMock()
    plugin._provider = mock_provider

    await plugin.on_unload()
    mock_provider.force_flush.assert_called_once()
    mock_provider.shutdown.assert_called_once()
    assert plugin._provider is None
    assert plugin._meter is None


@pytest.mark.asyncio
async def test_llm_call_timing_and_tokens() -> None:
    """4. pre→post LLM call records duration and token counters."""
    plugin = _make_plugin()
    clock = Clock()
    msgs = ["msg1"]

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_llm_call(msgs, model="gpt-5.4")
        clock.advance(0.5)
        await plugin.post_llm_call(
            msgs, None, {"prompt_tokens": 100, "completion_tokens": 50},
            model="gpt-5.4",
        )

    plugin._histograms["kt.llm.duration"].record.assert_called_once_with(
        500.0, {"model": "gpt-5.4"}
    )
    plugin._counters["kt.llm.calls"].add.assert_called_with(1, {"model": "gpt-5.4"})
    plugin._counters["kt.llm.tokens.prompt"].add.assert_called_with(
        100, {"model": "gpt-5.4"}
    )
    plugin._counters["kt.llm.tokens.completion"].add.assert_called_with(
        50, {"model": "gpt-5.4"}
    )


@pytest.mark.asyncio
async def test_llm_call_usage_none() -> None:
    """5. post_llm_call with usage=None does not crash."""
    plugin = _make_plugin()
    msgs = ["msg1"]

    await plugin.pre_llm_call(msgs, model="test-model")
    await plugin.post_llm_call(msgs, None, None, model="test-model")

    plugin._counters["kt.llm.calls"].add.assert_called_with(1, {"model": "test-model"})


@pytest.mark.asyncio
async def test_concurrent_llm_calls() -> None:
    """6. Two overlapping LLM calls tracked by different id(messages)."""
    plugin = _make_plugin()
    clock = Clock()
    msgs_a = ["a"]
    msgs_b = ["b"]

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_llm_call(msgs_a, model="m1")
        clock.advance(0.1)
        await plugin.pre_llm_call(msgs_b, model="m2")
        clock.advance(0.4)
        await plugin.post_llm_call(msgs_a, None, {}, model="m1")
        clock.advance(0.1)
        await plugin.post_llm_call(msgs_b, None, {}, model="m2")

    assert plugin._counters["kt.llm.calls"].add.call_count == 2
    assert plugin._start_times == {}


@pytest.mark.asyncio
async def test_tool_dispatch_counter() -> None:
    """7. pre_tool_dispatch increments the dispatches counter."""
    plugin = _make_plugin()
    call = SimpleNamespace(name="bash", args={}, raw="")
    await plugin.pre_tool_dispatch(call, _ctx())

    plugin._counters["kt.tool.dispatches"].add.assert_called_with(
        1, {"tool_name": "bash"}
    )


@pytest.mark.asyncio
async def test_tool_execute_timing_and_errors() -> None:
    """8. pre→post tool execute records duration; failure bumps error counter."""
    plugin = _make_plugin()
    clock = Clock()

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_tool_execute({"cmd": "ls"}, tool_name="bash", job_id="j1")
        clock.advance(0.3)
        result_ok = SimpleNamespace(success=True)
        await plugin.post_tool_execute(result_ok, tool_name="bash", job_id="j1")

    args, kwargs = plugin._histograms["kt.tool.duration"].record.call_args
    assert args[0] == pytest.approx(300.0)
    assert kwargs == {} or args[1] == {"tool_name": "bash"}
    plugin._counters["kt.tool.calls"].add.assert_called_with(1, {"tool_name": "bash"})
    plugin._counters["kt.tool.errors"].add.assert_not_called()

    # Failure path
    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_tool_execute({"cmd": "bad"}, tool_name="bash", job_id="j2")
        clock.advance(0.2)
        result_fail = SimpleNamespace(success=False)
        await plugin.post_tool_execute(result_fail, tool_name="bash", job_id="j2")

    plugin._counters["kt.tool.errors"].add.assert_called_with(1, {"tool_name": "bash"})


@pytest.mark.asyncio
async def test_tool_result_none() -> None:
    """9. post_tool_execute with result=None treats as success."""
    plugin = _make_plugin()
    await plugin.pre_tool_execute({"cmd": "ls"}, tool_name="bash", job_id="j3")
    await plugin.post_tool_execute(None, tool_name="bash", job_id="j3")

    plugin._counters["kt.tool.calls"].add.assert_called_with(1, {"tool_name": "bash"})
    plugin._counters["kt.tool.errors"].add.assert_not_called()


@pytest.mark.asyncio
async def test_subagent_run() -> None:
    """10. pre→post subagent run records duration, turns, and error on failure."""
    plugin = _make_plugin()
    clock = Clock()

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.pre_subagent_run(
            "do something", name="worker", job_id="j4"
        )
        clock.advance(1.0)
        result = SimpleNamespace(success=False, turns=7)
        await plugin.post_subagent_run(result, name="worker", job_id="j4")

    plugin._counters["kt.subagent.runs"].add.assert_called_with(1, {"name": "worker"})
    plugin._histograms["kt.subagent.duration"].record.assert_called_with(
        1000.0, {"name": "worker"}
    )
    plugin._histograms["kt.subagent.turns"].record.assert_called_with(
        7, {"name": "worker"}
    )
    plugin._counters["kt.subagent.errors"].add.assert_called_with(1, {"name": "worker"})


@pytest.mark.asyncio
async def test_compact_hooks() -> None:
    """11. on_compact_start/end increment counters and observe histogram."""
    plugin = _make_plugin()

    await plugin.on_compact_start(context_length=5000)
    plugin._counters["kt.compact.count"].add.assert_called_with(1, {})
    plugin._histograms["kt.compact.context_length"].record.assert_called_with(
        5000, {}
    )

    await plugin.on_compact_end(summary="compressed", messages_removed=12)
    plugin._histograms["kt.compact.messages_removed"].record.assert_called_with(
        12, {}
    )


@pytest.mark.asyncio
async def test_on_event() -> None:
    """12. on_event increments the events counter with event type."""
    plugin = _make_plugin()

    event = SimpleNamespace(type="tool_output")
    await plugin.on_event(event)
    plugin._counters["kt.events"].add.assert_called_with(
        1, {"event_type": "tool_output"}
    )

    # event=None defaults to "unknown"
    await plugin.on_event(None)
    plugin._counters["kt.events"].add.assert_called_with(1, {"event_type": "unknown"})


@pytest.mark.asyncio
async def test_on_interrupt() -> None:
    """13. on_interrupt increments the interrupts counter."""
    plugin = _make_plugin()

    await plugin.on_interrupt()
    plugin._counters["kt.interrupts"].add.assert_called_with(1, {})


@pytest.mark.asyncio
async def test_session_duration() -> None:
    """14. on_load sets session start; on_agent_stop records duration."""
    plugin = _make_plugin()
    clock = Clock(1000.0)

    with patch("kt_biome.plugins.otel_metrics.time.monotonic", clock):
        await plugin.on_load(_ctx("session-test"))
        clock.advance(42.0)
        await plugin.on_agent_stop()

    plugin._histograms["kt.agent.session.duration"].record.assert_called_with(
        42.0, {"agent": "session-test"}
    )


def test_metric_names_immutable() -> None:
    """15. All 12 counter names and 7 histogram names exist in the module defs."""
    counter_names = [name for name, _ in mod._COUNTER_DEFS]
    histogram_names = [name for name, _, _ in mod._HISTOGRAM_DEFS]

    assert len(counter_names) == 12
    assert len(histogram_names) == 7

    expected_counters = {
        "kt.llm.calls",
        "kt.llm.tokens.prompt",
        "kt.llm.tokens.completion",
        "kt.llm.tokens.cached",
        "kt.tool.calls",
        "kt.tool.dispatches",
        "kt.tool.errors",
        "kt.subagent.runs",
        "kt.subagent.errors",
        "kt.compact.count",
        "kt.events",
        "kt.interrupts",
    }
    expected_histograms = {
        "kt.llm.duration",
        "kt.tool.duration",
        "kt.subagent.duration",
        "kt.subagent.turns",
        "kt.compact.context_length",
        "kt.compact.messages_removed",
        "kt.agent.session.duration",
    }

    assert set(counter_names) == expected_counters
    assert set(histogram_names) == expected_histograms
