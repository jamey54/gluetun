"""Tests for the benchmark engine: candidates, ranking, orchestration."""

from contextlib import contextmanager
from typing import Any

import pytest

from vpn import apply as apply_module
from vpn import bench, cli, control
from vpn.apply import Verification

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def candidate(provider="surfshark", protocol="wireguard", country="Germany",
              city=None, *hosts: str) -> bench.Candidate:
    return bench.Candidate(provider, protocol, country, city, hosts)


def rows(*entries: tuple[str, str, str, str, str]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for provider, _protocol, _country, _city, _host in entries:
        out.setdefault(provider, [])
    for provider, protocol, country, city, host in entries:
        out[provider].append(
            {"country": country, "city": city, "hostname": host, "vpn": protocol}
        )
    return out


def baseline_doc() -> dict[str, Any]:
    return {"type": "wireguard", "provider": {"name": "surfshark"}}


def speed(mbits: float):
    def fake_measure(size_mb: int, timeout: int = 120) -> dict[str, float]:
        return {"mbits": mbits, "seconds": 1.0, "mbytes": float(size_mb)}

    return fake_measure


class Recorder:
    """Records (args, kwargs); optionally replays queued results."""

    def __init__(self, results: list[Any] | None = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.results = list(results or [])

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.results:
            item = self.results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return None


@pytest.fixture(autouse=True)
def happy_path(monkeypatch):
    """Defaults: route works; every candidate verifies with its own exit IP."""
    monkeypatch.setattr(bench, "get_settings", Recorder([baseline_doc()]))
    puts = Recorder()
    monkeypatch.setattr(apply_module, "put_settings", puts)
    monkeypatch.setattr(apply_module, "get_settings", lambda: baseline_doc())
    monkeypatch.setattr(bench, "current_exit_ip", lambda: None)
    seen: dict[str, str] = {}

    def fake_verify(sel: Any, prev_ip: str | None = None) -> Verification:
        ip = seen.setdefault(f"{sel.country}/{sel.city}", f"10.{len(seen)}.0.1")
        return Verification(ok=True, ip=ip)

    monkeypatch.setattr(bench, "verify", fake_verify)
    monkeypatch.setattr(bench, "measure", speed(10.0))
    monkeypatch.setattr(bench, "probe_hosts", Recorder([{}]))
    return puts


# ---------------------------------------------------------------------------
# build_candidates
# ---------------------------------------------------------------------------


def test_build_candidates_dedupes_locations_merges_hostnames():
    data = rows(
        ("surfshark", "wireguard", "Germany", "Berlin", "de1.x.com"),
        ("surfshark", "wireguard", "Germany", "Berlin", "de2.x.com"),
        ("surfshark", "wireguard", "Germany", "", "de3.x.com"),
    )
    result = bench.build_candidates(data)
    # sorted order: bare "Germany" (empty city) sorts before "Germany / Berlin"
    assert [(c.country, c.city) for c in result] == [
        ("Germany", None),
        ("Germany", "Berlin"),
    ]
    berlin = next(c for c in result if c.city == "Berlin")
    assert berlin.hostnames == ("de1.x.com", "de2.x.com")


def test_build_candidates_filters():
    data = rows(
        ("surfshark", "wireguard", "Germany", "", "a"),
        ("protonvpn", "wireguard", "Japan", "", "b"),
        ("surfshark", "openvpn", "France", "", "c"),
    )
    providers = [c.provider for c in bench.build_candidates(data, provider="surfshark")]
    assert providers == ["surfshark", "surfshark"]
    protocols = [c.protocol for c in bench.build_candidates(data, protocol="openvpn")]
    assert protocols == ["openvpn"]
    japan = bench.build_candidates(data, country="japan")  # case-insensitive
    assert len(japan) == 1 and japan[0].country == "Japan"


def test_build_candidates_preserves_sorted_order():
    data = rows(
        ("surfshark", "wireguard", "France", "Paris", "fr1"),
        ("surfshark", "wireguard", "Germany", "", "de1"),
        ("surfshark", "wireguard", "France", "Lyon", ""),
    )
    result = bench.build_candidates(data)
    assert [(c.country, c.city, c.hostnames) for c in result] == [
        ("France", "Lyon", ()),
        ("France", "Paris", ("fr1",)),
        ("Germany", None, ("de1",)),
    ]


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------


def test_best_latency_min_ignoring_unreachable():
    both = candidate("surfshark", "wireguard", "X", None, "h1", "h2")
    assert bench.best_latency(both, {"h1": 0.9, "h2": 0.5}) == 0.5
    assert bench.best_latency(both, {"h1": None, "h2": None}) is None
    assert bench.best_latency(candidate(), {}) is None  # no hostnames at all


def test_rank_unreachable_last_stable():
    r_fast = bench.BenchResult(candidate("surfshark", "wireguard", "Fast", None, "fast"))
    r_slow = bench.BenchResult(candidate("surfshark", "wireguard", "Slow", None, "slow"))
    r_dead = bench.BenchResult(candidate("surfshark", "wireguard", "Dead", None, "dead"))
    r_dead2 = bench.BenchResult(candidate("surfshark", "wireguard", "Dead2", None, "dead2"))
    by_host = {"fast": 0.9, "slow": 0.1, "dead": None, "dead2": None}
    ranked = bench.rank([r_dead, r_fast, r_dead2, r_slow], by_host)
    assert [r.candidate.location for r in ranked] == ["Slow", "Fast", "Dead", "Dead2"]


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def test_run_bench_connects_winner_by_default(monkeypatch):
    probes = {"fast": 0.05, "mid": 0.10, "slow": 0.20}
    monkeypatch.setattr(bench, "probe_hosts", Recorder([probes]))

    counter = {"n": 0}

    def fake_measure(size_mb: int, timeout: int = 120) -> dict[str, float]:
        counter["n"] += 1
        # screening runs latency-ranked (fast, mid, slow), speeds descending
        return {"mbits": 30.0 - 10.0 * (counter["n"] - 1 % 3), "seconds": 1.0,
                "mbytes": float(size_mb)}

    monkeypatch.setattr(bench, "measure", fake_measure)

    candidates = [
        candidate("surfshark", "wireguard", "Slow", None, "slow"),
        candidate("surfshark", "wireguard", "Mid", None, "mid"),
        candidate("surfshark", "wireguard", "Fast", None, "fast"),
    ]
    report = bench.run_bench(candidates, top=3, say=lambda *_: None)

    assert report.winner is not None
    assert report.winner.candidate.location == "Fast"
    assert not report.interrupted
    assert report.action.startswith("Connected to winner")


def test_run_bench_no_connect_restores_baseline(monkeypatch, happy_path):
    base = baseline_doc()
    monkeypatch.setattr(bench, "get_settings", Recorder([base]))
    report = bench.run_bench(
        [candidate("surfshark", "wireguard", "France", None, "fr")],
        connect_winner=False, say=lambda *_: None,
    )
    assert report.winner is not None
    assert report.action.startswith("Kept previous settings")
    assert any(args and args[0] is base for args, _ in happy_path.calls)


def test_run_bench_swap_failure_recorded(monkeypatch):
    """A failing hot-swap marks only that candidate and keeps benching."""
    # first PUT (the swap) fails; the later baseline restore succeeds
    puts = Recorder([control.ControlError(500, "tunnel restart timed out")])
    monkeypatch.setattr(apply_module, "put_settings", puts)
    monkeypatch.setattr(apply_module, "get_settings", lambda: baseline_doc())
    report = bench.run_bench(
        [candidate("surfshark", "openvpn", "Poland", "Warsaw", "pl1")],
        connect_winner=False, say=lambda *_: None,
    )
    assert report.winner is None
    assert report.results[0].error == "swap failed: tunnel restart timed out"
    assert "No working location found" in report.action


def test_run_bench_verification_failure_excluded(monkeypatch):
    monkeypatch.setattr(
        bench, "verify",
        lambda cand, prev_ip=None: Verification(ok=False, reason="leak"),
    )
    monkeypatch.setattr(bench, "measure", lambda size_mb, timeout=120: None)
    report = bench.run_bench(
        [candidate("surfshark", "wireguard", "Atlantis", None, "at")],
        connect_winner=False, say=lambda *_: None,
    )
    assert report.winner is None
    assert report.results[0].error == "leak"
    assert "No working location found" in report.action


def test_run_bench_geo_mismatch_still_benchmarked(monkeypatch):
    """Wrong-country exits (virtual locations) are flagged but speed-tested."""
    def fake_verify(sel: Any, prev_ip: str | None = None) -> Verification:
        return Verification(ok=True, ip="1.2.3.4", geo="Singapore")

    monkeypatch.setattr(bench, "verify", fake_verify)
    report = bench.run_bench(
        [candidate("surfshark", "wireguard", "Vietnam", None, "vn")],
        connect_winner=False, say=lambda *_: None,
    )
    result = report.results[0]
    assert result.scan_mbps == 10.0  # benchmarked despite geo mismatch
    assert result.actual_geo == "Singapore"


def test_run_bench_chains_previous_exit_ip(monkeypatch):
    seen_prev: list[str | None] = []
    counter = {"n": 0}

    def fake_verify(sel: Any, prev_ip: str | None = None) -> Verification:
        seen_prev.append(prev_ip)
        counter["n"] += 1
        return Verification(ok=True, ip=f"10.77.0.{counter['n']}")

    monkeypatch.setattr(bench, "current_exit_ip", lambda: "9.9.9.9")
    monkeypatch.setattr(bench, "verify", fake_verify)
    candidates = [
        candidate("surfshark", "wireguard", "L1", None, "h1"),
        candidate("surfshark", "wireguard", "L2", None, "h2"),
    ]
    bench.run_bench(candidates, top=2, connect_winner=False, say=lambda *_: None)
    # baseline snapshot feeds the first verify; each success re-anchors prev_ip
    assert seen_prev[0] == "9.9.9.9"
    assert seen_prev[1] == "10.77.0.1"
    assert seen_prev[2] == "10.77.0.2"


def test_run_bench_winner_adoption_stayed_passes_no_prev_ip(monkeypatch, happy_path):
    """Re-checking the just-tested winner must not exclude its own exit IP."""
    seen: list[tuple[str | None, str | None]] = []  # (requested country, prev_ip)

    def fake_verify(sel: Any, prev_ip: str | None = None) -> Verification:
        seen.append((sel.country, prev_ip))
        return Verification(ok=True, ip=f"10.{len(seen)}.0.1")

    monkeypatch.setattr(bench, "verify", fake_verify)
    report = bench.run_bench(
        [candidate("surfshark", "wireguard", "France", None, "fr")],
        say=lambda *_: None,
    )
    assert report.action.startswith("Connected to winner")
    # screen + final chain prev IPs; the stayed-on adoption call must not
    assert [prev for _, prev in seen] == [None, "10.1.0.1", None]
    assert seen[-1][0] == "France"


def test_run_bench_winner_adoption_after_swap_excludes_prev_exit(monkeypatch, happy_path):
    """When adoption hot-swaps back to the winner, its old exit stays excluded."""
    probes = {"fr": 0.05, "es": 0.10}
    monkeypatch.setattr(bench, "probe_hosts", Recorder([probes]))
    monkeypatch.setattr(  # scans tie so finals keep input order (Spain, France);
        bench, "measure",  # Spain 45 beats France 30 -> winner differs from last tested
        Recorder([{"mbits": m, "seconds": 1.0, "mbytes": 10.0}
                  for m in (10.0, 10.0, 45.0, 30.0)]),
    )
    seen: list[tuple[str | None, str | None]] = []

    def fake_verify(sel: Any, prev_ip: str | None = None) -> Verification:
        seen.append((sel.country, prev_ip))
        return Verification(ok=True, ip=f"10.{len(seen)}.0.1")

    monkeypatch.setattr(bench, "verify", fake_verify)
    candidates = [
        candidate("surfshark", "wireguard", "Spain", None, "es"),
        candidate("surfshark", "wireguard", "France", None, "fr"),
    ]
    report = bench.run_bench(candidates, say=lambda *_: None)
    assert report.action.startswith("Connected to winner")
    # France's final ran last, so prev_ip is France's exit when swapping to Spain
    assert seen[-1] == ("Spain", "10.4.0.1")


def test_run_bench_winner_adoption_reports_failed_recheck(monkeypatch, happy_path):
    calls = {"n": 0}

    def fake_verify(sel: Any, prev_ip: str | None = None) -> Verification:
        calls["n"] += 1
        if calls["n"] < 3:  # screen + final succeed; adoption re-check fails
            return Verification(ok=True, ip="10.9.9.9")
        return Verification(ok=False, reason="no public IP")

    monkeypatch.setattr(bench, "verify", fake_verify)
    report = bench.run_bench(
        [candidate("surfshark", "wireguard", "France", None, "fr")],
        say=lambda *_: None,
    )
    assert report.action.startswith("Stayed on")
    assert "no public IP" in report.action


def test_run_bench_interrupt_restores_partial_results(monkeypatch, happy_path):
    base = baseline_doc()
    monkeypatch.setattr(bench, "get_settings", Recorder([base]))

    def boom(size_mb: int, timeout: int = 120) -> dict[str, float]:
        raise KeyboardInterrupt()

    monkeypatch.setattr(bench, "measure", boom)
    report = bench.run_bench(
        [candidate("surfshark", "wireguard", "France", None, "fr")], say=lambda *_: None
    )
    assert report.interrupted is True
    assert report.winner is None
    assert report.action == "Restored previous settings."
    assert any(args and args[0] is base for args, _ in happy_path.calls)


def test_run_bench_reads_baseline_under_lock(monkeypatch, happy_path):
    events: list[str] = []

    @contextmanager
    def track_lock():
        events.append("lock")
        yield
        events.append("unlock")

    monkeypatch.setattr(bench, "swap_lock", track_lock)

    def fake_settings() -> dict[str, Any]:
        events.append("read")
        return baseline_doc()

    monkeypatch.setattr(bench, "get_settings", fake_settings)
    bench.run_bench(
        [candidate("surfshark", "wireguard", "France", None, "fr")],
        connect_winner=False, say=lambda *_: None,
    )
    assert events == ["lock", "read", "unlock"]


def test_run_bench_interrupt_during_adoption_restores(monkeypatch, happy_path):
    """Ctrl-C during the winner re-check must still restore the baseline."""
    base = baseline_doc()
    monkeypatch.setattr(bench, "get_settings", Recorder([base]))
    calls = {"n": 0}

    def fake_verify(sel: Any, prev_ip: str | None = None) -> Verification:
        calls["n"] += 1
        if calls["n"] < 3:  # screen + final succeed
            return Verification(ok=True, ip="10.8.8.8")
        raise KeyboardInterrupt()  # user aborts during the slow re-check

    monkeypatch.setattr(bench, "verify", fake_verify)
    report = bench.run_bench(
        [candidate("surfshark", "wireguard", "France", None, "fr")],
        say=lambda *_: None,
    )
    assert report.winner is not None
    assert any(args and args[0] is base for args, _ in happy_path.calls)
    assert "Interrupted" in report.action


def test_run_bench_limit_caps_candidates(monkeypatch):
    candidates = [candidate("surfshark", "wireguard", f"Land{i}", None, f"h{i}") for i in range(5)]
    report = bench.run_bench(candidates, limit=2, top=10, say=lambda *_: None)
    assert len(report.results) == 2


# ---------------------------------------------------------------------------
# display ordering
# ---------------------------------------------------------------------------


def test_sorted_results_finalists_first_failures_last():
    def loc(name: str) -> bench.Candidate:
        return candidate("surfshark", "wireguard", name)

    ok = bench.BenchResult(loc("A"), scan_mbps=5.0)
    final_slow = bench.BenchResult(loc("B"), scan_mbps=50.0, final_mbps=8.0)
    final_fast = bench.BenchResult(loc("C"), scan_mbps=40.0, final_mbps=90.0)
    dead = bench.BenchResult(loc("D"), error="verification failed")
    ordered = bench.sorted_results([ok, dead, final_slow, final_fast])
    assert [r.candidate.location for r in ordered] == ["C", "B", "A", "D"]


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_bench_end_to_end(monkeypatch):
    from click.testing import CliRunner

    data = rows(
        ("surfshark", "wireguard", "France", "", "fr1"),
        ("surfshark", "wireguard", "Japan", "", "jp1"),
    )
    monkeypatch.setattr(cli, "require_api_key", lambda: None)
    monkeypatch.setattr(cli, "container_running", lambda: True)
    monkeypatch.setattr(cli, "get_servers", lambda: data)
    monkeypatch.setattr(cli, "listable_servers", lambda d: d)
    monkeypatch.setattr(
        control, "get_settings",
        lambda: {"type": "wireguard", "provider": {"name": "surfshark"}},
    )

    probes = {"fr1": 0.05, "jp1": 0.20}
    monkeypatch.setattr(bench, "probe_hosts", Recorder([probes]))
    counter = {"n": 0}

    def fake_measure(size_mb: int, timeout: int = 120) -> dict[str, float]:
        counter["n"] += 1
        return {"mbits": 100.0 - counter["n"], "seconds": 1.0, "mbytes": float(size_mb)}

    monkeypatch.setattr(bench, "measure", fake_measure)

    result = CliRunner().invoke(cli.main, ["bench"])
    assert result.exit_code == 0, result.output
    # default scope = running pair; latency-ranked screening; winner France connected
    assert "Benchmarking 2 locations" in result.output
    assert "Connected to winner: surfshark/wireguard France" in result.output
    assert "France" in result.output and "Japan" in result.output


def test_cli_bench_requires_running_container(monkeypatch):
    from click.testing import CliRunner

    monkeypatch.setattr(cli, "require_api_key", lambda: None)
    monkeypatch.setattr(cli, "container_running", lambda: False)
    result = CliRunner().invoke(cli.main, ["bench"])
    assert result.exit_code != 0
    assert "not running" in result.output
