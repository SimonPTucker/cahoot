"""Tests for the admission policy."""

from __future__ import annotations

from cahoot.admission import AdmissionPolicy, decide


class TestOpenMode:
    def test_admits_any_agent(self) -> None:
        policy = AdmissionPolicy()  # default mode=open
        d = decide(policy, "hermes-main")
        assert d.admitted is True
        assert d.reason is None


class TestStrictMode:
    def test_admits_agent_in_allowlist(self) -> None:
        policy = AdmissionPolicy(
            mode="strict",
            allowed_ids=frozenset({"hermes-main", "openclaw-formatter-1"}),
        )
        assert decide(policy, "hermes-main").admitted is True

    def test_quarantines_unknown_agent(self) -> None:
        policy = AdmissionPolicy(
            mode="strict",
            allowed_ids=frozenset({"hermes-main"}),
        )
        d = decide(policy, "rogue-1")
        assert d.admitted is False
        assert d.reason and "rogue-1" in d.reason


class TestConfigIntegration:
    def test_load_config_with_admission_section(self, tmp_path) -> None:
        from cahoot.config import load_config

        cfg_path = tmp_path / "cahoot.toml"
        cfg_path.write_text(
            """
[cahoot]
room = "ops"

[cahoot.admission]
mode = "strict"
allowed_ids = ["external-helper"]

[[agents]]
id = "hermes-main"
role = "orchestrator"
kind = "hermes"
""".lstrip()
        )
        cfg = load_config(cfg_path)
        assert cfg.admission.mode == "strict"
        # In strict mode the listed [[agents]] are auto-added to allowlist.
        assert "hermes-main" in cfg.admission.allowed_ids
        assert "external-helper" in cfg.admission.allowed_ids
