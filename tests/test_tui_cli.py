"""Bare ``epoxy`` and ``epoxy tui`` behavior outside a terminal."""

from tests.harness import run_bare_cli


def test_bare_epoxy_prints_help_without_a_tty() -> None:
    """Scripts and pipes must never get a half-rendered TUI."""
    result = run_bare_cli([])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "tui" in result.output


def test_tui_refuses_to_run_without_a_terminal() -> None:
    result = run_bare_cli(["tui"])
    assert result.exit_code == 2
    assert "needs a terminal" in result.output


def test_tui_is_listed_in_help() -> None:
    result = run_bare_cli(["--help"])
    assert result.exit_code == 0
    assert "tui" in result.output
