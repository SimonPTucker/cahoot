"""Tests for the TOML config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from cahoot.config import ConfigError, load_config


def test_load_config_parses_agents_and_forwards_extra_options(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cahoot.toml"
    cfg_path.write_text(
        """
[cahoot]
room = "ops"
log_level = "DEBUG"

[[agents]]
id = "synth-1"
role = "test"
kind = "synthetic"
chatter_interval_s = 0.5

[[agents]]
id = "hermes-main"
role = "orchestrator"
kind = "hermes"
version = "0.9.4"
""".lstrip()
    )
    cfg = load_config(cfg_path)
    assert cfg.room == "ops"
    assert cfg.log_level == "DEBUG"
    assert len(cfg.agents) == 2
    assert cfg.agents[0].kind == "synthetic"
    assert cfg.agents[0].options == {"chatter_interval_s": 0.5}
    assert cfg.agents[1].version == "0.9.4"
    assert cfg.agents[1].options == {}


def test_missing_required_field_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cahoot.toml"
    cfg_path.write_text(
        """
[[agents]]
role = "test"
kind = "synthetic"
""".lstrip()
    )
    with pytest.raises(ConfigError):
        load_config(cfg_path)
