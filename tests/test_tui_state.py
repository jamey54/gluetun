"""Pure view-model helpers: filtering, targets, and safe rendering."""

import pytest

from epoxy.apply import Selection
from epoxy.bench import BenchReport, BenchResult, Candidate
from epoxy.tui.backend import CredentialChoice, InstanceStatus, InstanceSummary
from epoxy.tui.state import (
    bench_rows,
    credential_markup,
    detail_markup,
    filter_summaries,
    parse_optional_int,
    parse_publish_specs,
    resolve_targets,
)

ROW = InstanceSummary(
    name="alpha",
    state="running",
    selection=Selection("surfshark", "wireguard", "Netherlands", "Amsterdam"),
    control_port=8000,
    consumers=("web",),
    started_at="2026-01-01T00:00:00Z",
    forwarded_ports=(),
    saved_selection=None,
)
STOPPED = InstanceSummary(
    name="beta",
    state="stopped",
    selection=None,
    control_port=None,
    consumers=(),
    started_at=None,
    forwarded_ports=(),
    saved_selection=None,
)


def test_filter_matches_any_visible_field_and_is_accent_insensitive() -> None:
    rows = [ROW, STOPPED]
    assert filter_summaries(rows, "") == rows
    assert filter_summaries(rows, "amsterdam") == [ROW]
    assert filter_summaries(rows, "SURFSHARK wireguard") == [ROW]
    assert filter_summaries(rows, "web") == [ROW]
    assert filter_summaries(rows, "missing") == []


def test_resolve_targets_prefers_selection_then_cursor() -> None:
    assert resolve_targets({"beta", "alpha"}, "beta", ["alpha", "beta"]) == ["alpha", "beta"]
    assert resolve_targets(set(), "beta", ["alpha", "beta"]) == ["beta"]
    assert resolve_targets(set(), "gamma", ["alpha", "beta"]) == []
    assert resolve_targets(set(), None, ["alpha"]) == []


def test_parse_publish_specs_accepts_commas_and_spaces() -> None:
    assert parse_publish_specs("8080:80, 5353:53/udp") == ("8080:80", "5353:53/udp")
    assert parse_publish_specs("   ") == ()


def test_parse_optional_int_reports_friendly_errors() -> None:
    assert parse_optional_int("", "Port") is None
    assert parse_optional_int(" 8123 ", "Port") == 8123
    with pytest.raises(ValueError, match="must be a number"):
        parse_optional_int("abc", "Port")
    with pytest.raises(ValueError, match="at least 1"):
        parse_optional_int("0", "Port")


def test_credentials_render_names_only_never_values() -> None:
    choices = [
        CredentialChoice("surfshark", "wireguard", ()),
        CredentialChoice("protonvpn", "openvpn", ("PROTONVPN_OPENVPN_USER",)),
    ]
    markup = credential_markup(choices)
    assert "surfshark / wireguard" in markup
    assert "missing PROTONVPN_OPENVPN_USER" in markup
    assert "secret-value" not in markup


def test_detail_markup_includes_state_verification_and_errors() -> None:
    status = InstanceStatus(
        name="alpha",
        state="running",
        image="qmcgaw/gluetun:latest",
        selection=ROW.selection,
        drift=True,
        control_port=8000,
        control_enabled=True,
        exit_ip="203.0.113.7",
        exit_country="NL",
        leak=False,
        verified=True,
        last_error="probe failed",
        forwarded_ports=("8080:80",),
        consumers=("web",),
        saved_selection=None,
        tunnel="running",
        dns="running",
        port_forward=None,
    )
    markup = detail_markup(status)
    assert "alpha" in markup
    assert "verified" in markup
    assert "runtime selection differs" in markup
    assert "probe failed" in markup
    assert "8080:80" in markup


def test_bench_rows_mark_the_winner_and_keep_failures_last() -> None:
    winner_candidate = Candidate("surfshark", "wireguard", "Netherlands", "Amsterdam")
    loser_candidate = Candidate("protonvpn", "openvpn", "Germany", "Berlin")
    winner = BenchResult(winner_candidate, latency_s=0.1, scan_mbps=50, final_mbps=100)
    loser = BenchResult(loser_candidate, latency_s=0.2, error="timeout")
    report = BenchReport(results=[loser, winner], winner=winner)
    rows = bench_rows(report)
    assert rows[0][3].startswith("★ ")
    assert rows[0][6] == "100.0"
    assert rows[1][7] == "timeout"
