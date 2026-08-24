"""Tests for the benchmark engine: candidates, ranking, orchestration."""

from typing import Any

import pytest

from vpn import bench, control


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

    def argss(self) -> list[tuple[Any, ...]]:
        return [args for args, _ in self.calls]


@pytest.fixture(autouse=True)
def happy_path(monkeypatch):
    """Defaults: route works, every candidate verifies and downloads at 10 Mbit/s."""
    monkeypatch.setattr(bench, "get_settings", Recorder([baseline_doc()]))
    puts = Recorder()
    monkeypatch.setattr(bench, "put_settings", puts)
    monkeypatch.setattr(bench, "fetch_ip_info", Recorder([[{"country": "DE"}]]))
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


def test_run_bench_falls_back_to_recreate_on_404(monkeypatch):
    monkeypatch.setattr(bench, "get_settings", Recorder([baseline_doc()]))

    def always_404(doc: dict[str, Any]) -> str:
        raise control.ControlError(404, "404 page not found")

    monkeypatch.setattr(bench, "put_settings", always_404)
    recreates = Recorder()
    monkeypatch.setattr(bench, "compose", recreates)

    report = bench.run_bench(
        [candidate("surfshark", "openvpn", "Poland", "Warsaw", "pl1")],
        say=lambda *_: None,
    )
    assert report.winner is not None  # recreate path still benchmarks fine
    commands = recreates.argss()
    assert commands, "compose recreate expected"
    assert "--force-recreate" in commands[0]


def test_run_bench_verification_failure_excluded(monkeypatch):
    monkeypatch.setattr(bench, "fetch_ip_info", Recorder())
    monkeypatch.setattr(bench, "measure", lambda size_mb, timeout=120: None)
    report = bench.run_bench(
        [candidate("surfshark", "wireguard", "Atlantis", None, "at")],
        connect_winner=False, say=lambda *_: None,
    )
    assert report.winner is None
    assert report.results[0].error == "verification failed"
    assert "No working location found" in report.action


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


def test_run_bench_limit_caps_candidates(monkeypatch):
    candidates = [candidate("surfshark", "wireguard", f"Land{i}", None, f"h{i}") for i in range(5)]
    report = bench.run_bench(candidates, limit=2, top=10, say=lambda *_: None)
    assert len(report.results) == 2


def test_run_bench_old_image_restore_recreates_original(monkeypatch):
    """Without the settings route, restore means recreating the original location."""
    monkeypatch.setattr(bench, "get_settings", Recorder([baseline_doc()]))
    recreates = Recorder()
    monkeypatch.setattr(bench, "compose", recreates)
    from vpn.docker import CurrentVpn

    original = CurrentVpn(provider="protonvpn", protocol="wireguard", countries="Japan")
    report = bench.run_bench(
        [candidate("surfshark", "wireguard", "France", None, "fr")],
        connect_winner=False,
        settings_route=False,
        original=original,
        say=lambda *_: None,
    )
    assert "Kept previous settings" in report.action
    commands = recreates.argss()
    assert commands and "--force-recreate" in commands[0]
    # last recreate is the restore of the original location
    env = recreates.calls[-1][1].get("env_overrides", {})
    assert env["VPN_SERVICE_PROVIDER"] == "protonvpn"
    assert env["SERVER_COUNTRIES"] == "Japan"


def test_run_bench_old_image_interrupt_recreates_original(monkeypatch):
    monkeypatch.setattr(bench, "get_settings", Recorder([baseline_doc()]))
    recreates = Recorder()
    monkeypatch.setattr(bench, "compose", recreates)

    def boom(size_mb: int, timeout: int = 120) -> dict[str, float]:
        raise KeyboardInterrupt()

    monkeypatch.setattr(bench, "measure", boom)
    from vpn.docker import CurrentVpn

    original = CurrentVpn(provider="surfshark", protocol="wireguard", countries="Iceland")
    report = bench.run_bench(
        [candidate("surfshark", "wireguard", "France", None, "fr")],
        settings_route=False,
        original=original,
        say=lambda *_: None,
    )
    assert report.interrupted and report.action == "Restored previous settings."
    env = recreates.calls[-1][1].get("env_overrides", {})
    assert env["SERVER_COUNTRIES"] == "Iceland"


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
