"""Tests for the startup splash banner."""

from __future__ import annotations

import pytest

from cahoot.banner import (
    BANNER_ART,
    _ansi_fg,
    _gradient,
    render,
    supports_truecolor,
)


class TestGradient:
    def test_endpoints_returned_exactly(self) -> None:
        top = (230, 81, 0)
        bottom = (255, 213, 79)
        stops = _gradient(top, bottom, 4)
        assert stops[0] == top
        assert stops[-1] == bottom
        assert len(stops) == 4

    def test_intermediate_values_monotonic(self) -> None:
        # All three channels should change monotonically along the gradient.
        top = (230, 81, 0)
        bottom = (255, 213, 79)
        stops = _gradient(top, bottom, 18)
        for c in range(3):
            channel = [s[c] for s in stops]
            assert channel == sorted(channel), f"channel {c} not monotonic"

    def test_n_equals_one_returns_top_only(self) -> None:
        assert _gradient((10, 20, 30), (100, 100, 100), 1) == [(10, 20, 30)]


class TestRender:
    def test_plain_render_contains_art(self) -> None:
        out = render(use_color=False)
        # Every non-blank line of the art appears in the output.
        for line in BANNER_ART.splitlines():
            if line.strip():
                assert line in out

    def test_plain_render_contains_credit(self) -> None:
        out = render(use_color=False)
        assert "by Kenjin" in out
        assert "2026" in out
        assert "v0.1.0" in out

    def test_plain_render_has_no_ansi(self) -> None:
        out = render(use_color=False)
        assert "\x1b[" not in out

    def test_color_render_has_ansi(self) -> None:
        out = render(use_color=True)
        assert "\x1b[38;2;230;81;0m" in out  # top endpoint
        assert "\x1b[38;2;255;213;79m" in out  # bottom endpoint
        assert "\x1b[0m" in out  # reset

    def test_ansi_fg_format(self) -> None:
        assert _ansi_fg(255, 0, 0) == "\x1b[38;2;255;0;0m"


class TestColorDetection:
    def test_no_color_env_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv("COLORTERM", "truecolor")

        class FakeTTY:
            def isatty(self) -> bool:
                return True

        assert supports_truecolor(FakeTTY()) is False

    def test_non_tty_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("COLORTERM", "truecolor")

        class FakeNonTTY:
            def isatty(self) -> bool:
                return False

        assert supports_truecolor(FakeNonTTY()) is False

    def test_truecolor_colorterm_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("COLORTERM", "truecolor")

        class FakeTTY:
            def isatty(self) -> bool:
                return True

        assert supports_truecolor(FakeTTY()) is True
