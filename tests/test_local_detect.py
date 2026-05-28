"""Tests for cahoot/local_detect.py.

Probes are pure functions over ``shutil.which`` + a couple of subprocess
calls, so we mock those rather than relying on whatever's installed on
the test runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cahoot import local_detect as ld

# ---------------------------------------------------------------------------
# Per-runtime probes
# ---------------------------------------------------------------------------


class TestHermesProbe:
    def test_unavailable_when_uvx_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ld, "_which", lambda cmd: None)
        probe = ld.detect_hermes()
        assert probe.kind == "hermes"
        assert probe.available is False
        # Notes should carry the uv install hint so the user can self-serve.
        assert any("astral.sh/uv/install.sh" in n for n in probe.notes)

    def test_available_when_uvx_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ld, "_which", lambda cmd: "/opt/homebrew/bin/uvx" if cmd == "uvx" else None
        )
        monkeypatch.setattr(ld, "_quick_version", lambda *_a, **_k: "uv 0.9.7")
        probe = ld.detect_hermes()
        assert probe.available is True
        assert probe.version == "uv 0.9.7"
        # Note explains that Hermes is fetched on demand.
        assert any("fetched on first launch" in n for n in probe.notes)


class TestOpenClawProbe:
    def test_unavailable_when_cli_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ld, "_which", lambda cmd: None)
        probe = ld.detect_openclaw()
        assert probe.available is False
        # Note should tell the user how to install + onboard.
        assert any("brew install openclaw" in n for n in probe.notes)
        assert any("openclaw onboard" in n for n in probe.notes)

    def test_available_without_token_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            ld,
            "_which",
            lambda cmd: "/usr/local/bin/openclaw" if cmd == "openclaw" else None,
        )
        monkeypatch.setattr(ld, "_quick_version", lambda *_a, **_k: "openclaw 1.4.2")
        # Point the token-file probe at a temp dir with no token.
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        probe = ld.detect_openclaw()
        assert probe.available is True
        assert probe.version == "openclaw 1.4.2"
        # No suggested token file when none exists.
        assert "token_file" not in probe.suggested_kwargs
        assert any("no ~/.openclaw/main.token" in n for n in probe.notes)

    def test_available_with_token_file_suggests_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            ld,
            "_which",
            lambda cmd: "/usr/local/bin/openclaw" if cmd == "openclaw" else None,
        )
        monkeypatch.setattr(ld, "_quick_version", lambda *_a, **_k: "openclaw 1.4.2")
        fake_home = tmp_path / "home"
        (fake_home / ".openclaw").mkdir(parents=True)
        (fake_home / ".openclaw" / "main.token").write_text("secret")
        monkeypatch.setenv("HOME", str(fake_home))
        probe = ld.detect_openclaw()
        assert probe.suggested_kwargs.get("token_file", "").endswith(".openclaw/main.token")


class TestSyntheticProbe:
    def test_always_available(self) -> None:
        probe = ld.detect_synthetic()
        assert probe.kind == "synthetic"
        assert probe.available is True
        assert any("built-in" in n for n in probe.notes)


# ---------------------------------------------------------------------------
# Aggregation + picking
# ---------------------------------------------------------------------------


def _mk(kind: ld.RuntimeKind, available: bool, **extras) -> ld.RuntimeProbe:
    return ld.RuntimeProbe(kind=kind, available=available, **extras)


class TestPickDefault:
    def test_single_real_runtime_is_picked(self) -> None:
        chosen = ld.pick_default(
            [
                _mk("hermes", True),
                _mk("openclaw", False),
                _mk("synthetic", True),
            ]
        )
        assert chosen.kind == "hermes"

    def test_multiple_real_runtimes_requires_explicit_kind(self) -> None:
        with pytest.raises(ld.RuntimeUnavailableError) as exc:
            ld.pick_default(
                [
                    _mk("hermes", True),
                    _mk("openclaw", True),
                    _mk("synthetic", True),
                ]
            )
        msg = str(exc.value)
        assert "multiple runtimes" in msg.lower()
        assert "hermes" in msg and "openclaw" in msg

    def test_no_real_runtime_raises_with_hints(self) -> None:
        with pytest.raises(ld.RuntimeUnavailableError) as exc:
            ld.pick_default(
                [
                    _mk("hermes", False, notes=("uvx not on PATH",)),
                    _mk("openclaw", False, notes=("openclaw not installed",)),
                    _mk("synthetic", True),
                ]
            )
        msg = str(exc.value)
        # The error surfaces install hints from each unavailable runtime.
        assert "uvx not on PATH" in msg
        assert "openclaw not installed" in msg
        # And mentions synthetic as the escape hatch.
        assert "synthetic" in msg

    def test_synthetic_alone_is_not_auto_picked(self) -> None:
        # Even though synthetic is available, pick_default should refuse
        # so the user has to opt in explicitly.
        with pytest.raises(ld.RuntimeUnavailableError):
            ld.pick_default(
                [
                    _mk("hermes", False),
                    _mk("openclaw", False),
                    _mk("synthetic", True),
                ]
            )


class TestFormatReport:
    def test_lists_each_kind_with_status_marker(self) -> None:
        report = ld.format_report(
            [
                _mk("hermes", True, version="uv 0.9.7"),
                _mk("openclaw", False, notes=("openclaw not on PATH",)),
                _mk("synthetic", True),
            ]
        )
        assert "✓ hermes" in report
        assert "✗ openclaw" in report
        assert "✓ synthetic" in report
        assert "uv 0.9.7" in report
        assert "openclaw not on PATH" in report

    def test_tip_when_single_real_runtime(self) -> None:
        report = ld.format_report(
            [_mk("hermes", True), _mk("openclaw", False), _mk("synthetic", True)]
        )
        assert "auto-pick" in report.lower()
        assert "hermes" in report

    def test_tip_when_multiple_real_runtimes(self) -> None:
        report = ld.format_report(
            [_mk("hermes", True), _mk("openclaw", True), _mk("synthetic", True)]
        )
        assert "--kind" in report
        assert "disambiguate" in report.lower()

    def test_tip_when_no_real_runtimes(self) -> None:
        report = ld.format_report(
            [_mk("hermes", False), _mk("openclaw", False), _mk("synthetic", True)]
        )
        assert "Hermes or OpenClaw" in report
        assert "--kind synthetic" in report


class TestDetectAllOrdering:
    def test_returns_hermes_openclaw_synthetic_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ld, "_which", lambda _cmd: None)
        probes = ld.detect_all()
        assert [p.kind for p in probes] == ["hermes", "openclaw", "synthetic"]
