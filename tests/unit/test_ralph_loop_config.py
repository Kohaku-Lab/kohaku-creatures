"""Tests for the kt-biome `ralph_loop` terrarium and its two creatures.

Validates that the terrarium + both creature configs load cleanly
(including the `base_config` inheritance from `general`), that the
`work-log` channel is declared, and that the worker prompt carries
the stop sentinel. No agents are actually started — this is a pure
config-parsing sanity check, following the same pattern as
`tests/unit/test_unified_config.py`.
"""

from pathlib import Path

import pytest
import yaml

from kohakuterrarium.core.config import load_agent_config
from kohakuterrarium.terrarium.config import load_terrarium_config

BIOME_ROOT = Path(__file__).resolve().parents[2]
TERRARIUM_DIR = BIOME_ROOT / "terrariums" / "ralph_loop"
INIT_DIR = BIOME_ROOT / "creatures" / "ralph_initializer"
WORKER_DIR = BIOME_ROOT / "creatures" / "ralph_worker"

pytestmark = pytest.mark.skipif(
    not TERRARIUM_DIR.exists() or not INIT_DIR.exists() or not WORKER_DIR.exists(),
    reason="kt-biome ralph_loop terrarium not present",
)


class TestRalphLoopTerrarium:
    """The terrarium config parses and has the expected topology."""

    def test_terrarium_loads(self):
        config = load_terrarium_config(TERRARIUM_DIR)
        assert config.name == "ralph_loop"

    def test_two_creatures(self):
        config = load_terrarium_config(TERRARIUM_DIR)
        names = {c.name for c in config.creatures}
        assert names == {"ralph_initializer", "ralph_worker"}

    def test_no_root_agent(self):
        """Ralph loop deliberately runs without a root agent."""
        config = load_terrarium_config(TERRARIUM_DIR)
        assert config.root is None

    def test_work_log_channel_declared(self):
        config = load_terrarium_config(TERRARIUM_DIR)
        channel_names = {ch.name for ch in config.channels}
        assert "work-log" in channel_names

    def test_work_log_is_broadcast(self):
        config = load_terrarium_config(TERRARIUM_DIR)
        work_log = next(ch for ch in config.channels if ch.name == "work-log")
        assert work_log.channel_type == "broadcast"

    def test_worker_listens_on_work_log(self):
        """The worker must listen on work-log — that's what makes the loop loop."""
        config = load_terrarium_config(TERRARIUM_DIR)
        worker = next(c for c in config.creatures if c.name == "ralph_worker")
        assert "work-log" in worker.listen_channels
        assert "work-log" in worker.send_channels

    def test_initializer_sends_to_work_log(self):
        config = load_terrarium_config(TERRARIUM_DIR)
        init = next(c for c in config.creatures if c.name == "ralph_initializer")
        assert "work-log" in init.send_channels

    def test_creatures_inherit_from_biome_creatures(self):
        """Both creatures carry a base_config reference to their biome creature."""
        config = load_terrarium_config(TERRARIUM_DIR)
        for creature in config.creatures:
            assert "base_config" in creature.config_data


class TestRalphCreatureConfigs:
    """Both creature configs load via `load_agent_config` with `base: general`."""

    def test_initializer_loads(self):
        cfg = load_agent_config(INIT_DIR)
        assert cfg.name == "ralph_initializer"

    def test_initializer_inherits_general_tools(self):
        """Inheriting from `general` should give the initializer its toolset."""
        cfg = load_agent_config(INIT_DIR)
        tool_names = {t.name for t in cfg.tools}
        # A handful of core `general` tools that the initializer relies on.
        for needed in {"write", "read", "bash", "send_message"}:
            assert needed in tool_names, f"missing inherited tool: {needed}"

    def test_worker_loads(self):
        cfg = load_agent_config(WORKER_DIR)
        assert cfg.name == "ralph_worker"

    def test_worker_inherits_general_tools(self):
        cfg = load_agent_config(WORKER_DIR)
        tool_names = {t.name for t in cfg.tools}
        for needed in {"bash", "read", "write", "edit", "grep", "glob", "send_message"}:
            assert needed in tool_names, f"missing inherited tool: {needed}"


class TestRalphPrompts:
    """System prompts encode the stop sentinel and role contract."""

    def test_worker_prompt_mentions_terminus(self):
        prompt_path = WORKER_DIR / "prompts" / "system.md"
        text = prompt_path.read_text(encoding="utf-8")
        assert "TERMINUS" in text, "worker prompt must mention the stop sentinel"

    def test_worker_prompt_mentions_stop(self):
        prompt_path = WORKER_DIR / "prompts" / "system.md"
        text = prompt_path.read_text(encoding="utf-8")
        assert "STOP" in text, "worker prompt must describe the STOP sentinel"

    def test_initializer_prompt_mentions_init_done(self):
        prompt_path = INIT_DIR / "prompts" / "system.md"
        text = prompt_path.read_text(encoding="utf-8")
        assert "INIT_DONE" in text

    def test_initializer_prompt_describes_three_files(self):
        prompt_path = INIT_DIR / "prompts" / "system.md"
        text = prompt_path.read_text(encoding="utf-8")
        for needed in {"AGENTS.md", "progress.md", "NOTES.md"}:
            assert needed in text, f"initializer prompt missing {needed}"


class TestRalphManifest:
    """The sibling manifest fragment parses and lists the right entries."""

    def test_manifest_parses(self):
        manifest_path = TERRARIUM_DIR / "manifest.yaml"
        assert manifest_path.exists(), "manifest.yaml fragment is missing"
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        assert "terrariums" in data
        assert "creatures" in data

    def test_manifest_lists_both_creatures(self):
        manifest_path = TERRARIUM_DIR / "manifest.yaml"
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        creature_names = {c["name"] for c in data["creatures"]}
        assert creature_names == {"ralph_initializer", "ralph_worker"}

    def test_manifest_terrarium_uses_both_creatures(self):
        manifest_path = TERRARIUM_DIR / "manifest.yaml"
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        terr = next(t for t in data["terrariums"] if t["name"] == "ralph_loop")
        assert set(terr["uses"]) == {"ralph_initializer", "ralph_worker"}
