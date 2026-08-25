"""Location benchmarking: latency prescreen, screened and final speed stages.

Candidates are unique (provider, protocol, country, city) locations from the
server cache. Each test hot-swaps through the runtime config engine
(vpn.apply), proves the exit IP actually moved (leak-first verification),
then downloads through the tunnel. The winner is connected by default;
Ctrl-C or --no-connect restores the pre-bench settings document instead.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from vpn.apply import Selection, apply_location, restore_settings, verify
from vpn.control import ControlError, get_settings
from vpn.ipinfo import current_exit_ip
from vpn.latency import probe_hosts
from vpn.servers import ServerRow, sorted_server_rows
from vpn.speedtest import DOWNLOAD_TIMEOUT_S, measure
from vpn.textutil import fold

DEFAULT_TOP = 12
DEFAULT_SCAN_SIZE_MB = 10
DEFAULT_FINAL_SIZE_MB = 25
FINALISTS = 3

SCAN_TIMEOUT_S = 90


@dataclass(frozen=True)
class Candidate:
    """One unique location to benchmark."""

    provider: str
    protocol: str
    country: str
    city: str | None
    hostnames: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.provider, self.protocol, fold(self.country), fold(self.city or ""))

    @property
    def location(self) -> str:
        return self.country + (f" / {self.city}" if self.city else "")

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.protocol} {self.location}"

    @property
    def selection(self) -> Selection:
        """The equivalent runtime Selection for this location."""
        return Selection(self.provider, self.protocol, self.country, self.city)


@dataclass
class BenchResult:
    """Outcome for one candidate across the bench stages."""

    candidate: Candidate
    latency_s: float | None = None
    scan_mbps: float | None = None
    final_mbps: float | None = None
    error: str | None = None
    actual_geo: str | None = None  # exit country when it differs from the request


@dataclass
class BenchReport:
    results: list[BenchResult] = field(default_factory=list)
    baseline: dict[str, Any] = field(default_factory=dict)
    winner: BenchResult | None = None
    interrupted: bool = False
    action: str = ""


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def build_candidates(
    by_provider: dict[str, list[ServerRow]],
    provider: str | None = None,
    protocol: str | None = None,
    country: str | None = None,
) -> list[Candidate]:
    """Dedupe cached rows into unique locations, filtered, in sorted order."""
    candidates: dict[tuple[str, str, str, str], Candidate] = {}
    hosts: dict[tuple[str, str, str, str], list[str]] = {}
    for prov, prot, ctry, city, hostname in sorted_server_rows(by_provider):
        if provider and prov != provider.lower():
            continue
        if protocol and prot != protocol.lower():
            continue
        if country and fold(ctry) != fold(country):
            continue
        candidate = Candidate(prov, prot, ctry, city or None)
        entry = candidates.setdefault(candidate.key, candidate)
        seen = hosts.setdefault(entry.key, [])
        if hostname and hostname not in seen:
            seen.append(hostname)
    return [replace(c, hostnames=tuple(hosts[c.key])) for c in candidates.values()]


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def best_latency(candidate: Candidate, by_host: dict[str, float | None]) -> float | None:
    """Fastest probe among the location's hostnames; None when all unreachable."""
    reachable = [
        latency
        for h in candidate.hostnames
        if (latency := by_host.get(h)) is not None
    ]
    return min(reachable) if reachable else None


def rank(results: list[BenchResult], by_host: dict[str, float | None]) -> list[BenchResult]:
    """Latency-sorted copy; unreachable locations keep input order at the end."""

    def key(result: BenchResult) -> tuple[bool, float]:
        latency = best_latency(result.candidate, by_host)
        return (latency is None, float("inf") if latency is None else latency)

    return sorted(results, key=key)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def run_bench(
    candidates: list[Candidate],
    *,
    top: int = DEFAULT_TOP,
    limit: int = 0,
    scan_size_mb: int = DEFAULT_SCAN_SIZE_MB,
    final_size_mb: int = DEFAULT_FINAL_SIZE_MB,
    connect_winner: bool = True,
    say: Callable[[str], None] = click.echo,
) -> BenchReport:
    """Run all bench stages; connect the winner unless asked otherwise."""
    report = BenchReport(baseline=get_settings())
    tested = candidates[:limit] if limit > 0 else candidates
    say(f"Benchmarking {len(tested)} locations "
        f"(screening top {min(top, len(tested))}, finals {FINALISTS}).")

    hosts = sorted({h for c in tested for h in c.hostnames})
    say(f"Probing {len(hosts)} hostnames...")
    by_host = probe_hosts(hosts)

    results = [BenchResult(candidate=c) for c in tested]
    for result in results:
        result.latency_s = best_latency(result.candidate, by_host)
    ranked = rank(results, by_host)

    current: Candidate | None = None
    prev_ip = current_exit_ip()

    def test_stage(pool: list[BenchResult], stage: str, size_mb: int, timeout: int) -> None:
        nonlocal current, prev_ip
        for i, result in enumerate(pool, 1):
            candidate = result.candidate
            say(f"[{i}/{len(pool)}] {candidate.label} ... swapping")
            try:
                apply_location(candidate.selection)
            except ControlError as exc:
                result.error = f"swap failed: {exc.message}"
                continue
            current = candidate
            verdict = verify(candidate.selection, prev_ip)
            if not verdict.ok:
                result.error = verdict.reason
                continue
            prev_ip = verdict.ip
            if verdict.geo:
                result.actual_geo = verdict.geo
                say(f"    note: exits via {verdict.geo} (requested {candidate.country})")
            downloaded = measure(size_mb, timeout=timeout)
            if not downloaded:
                result.error = "download failed"
                continue
            mbits = downloaded["mbits"]
            if stage == "screen":
                result.scan_mbps = mbits
            else:
                result.final_mbps = mbits
            say(f"    ↓ {mbits:.1f} Mbit/s")

    try:
        screen_pool = ranked[: max(top, 0)]
        say(f"Screening top {len(screen_pool)} ({scan_size_mb} MB each)...")
        test_stage(screen_pool, "screen", scan_size_mb, SCAN_TIMEOUT_S)

        finalists = sorted(
            (r for r in results if r.scan_mbps is not None),
            key=lambda r: r.scan_mbps or 0.0,
            reverse=True,
        )[:FINALISTS]
        if finalists:
            say(f"Finals ({final_size_mb} MB each): "
                + ", ".join(r.candidate.location for r in finalists))
            test_stage(finalists, "final", final_size_mb, DOWNLOAD_TIMEOUT_S)

        screened = [r for r in results if r.scan_mbps is not None]
        finished = [r for r in results if r.final_mbps is not None]
        report.winner = (
            max(finished, key=lambda r: r.final_mbps or 0.0, default=None)
            or max(screened, key=lambda r: r.scan_mbps or 0.0, default=None)
        )
    except KeyboardInterrupt:
        report.interrupted = True
        say("Interrupted.")

    try:
        if report.interrupted or not connect_winner or not report.winner:
            restore_settings(report.baseline)
            report.action = (
                "Restored previous settings." if report.interrupted
                else "No working location found; restored previous settings."
                if not report.winner
                else f"Kept previous settings — winner was {report.winner.candidate.label}."
            )
        else:
            winner = report.winner.candidate
            did_swap = False
            if current != winner:
                apply_location(winner.selection)
                did_swap = True
            # Exclude the pre-swap exit only when we actually moved; otherwise
            # the tunnel still exits via the winner itself, which must count.
            check = verify(winner.selection, prev_ip if did_swap else None)
            verb = "Swapped to" if did_swap else "Stayed on"
            if check.ok:
                report.action = f"Connected to winner: {winner.label}"
            else:
                report.action = f"{verb} {winner.label}, but re-check failed ({check.reason})"
    except ControlError as exc:
        report.action += f" (restore/connect failed: {exc.message})"

    report.results = results
    return report


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def sorted_results(results: list[BenchResult]) -> list[BenchResult]:
    """Finalists fastest-first, then screened, then the rest; failures last."""

    def sort_key(r: BenchResult) -> tuple[int, float, bool, float]:
        throughput = r.final_mbps or r.scan_mbps or 0.0
        tier = 0 if r.final_mbps else 1 if r.scan_mbps else 2
        return (tier, -throughput, r.latency_s is None, r.latency_s or 0.0)

    return sorted(results, key=sort_key)


def print_report(report: BenchReport) -> None:
    """Rich table of all results, fastest first."""
    table = Table(box=None, padding=(0, 1, 0, 0), header_style="bold")
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Provider", style="cyan", no_wrap=True)
    table.add_column("Protocol", style="dim", no_wrap=True)
    table.add_column("Location", no_wrap=True)
    table.add_column("Latency", justify="right", no_wrap=True)
    table.add_column("Scan ↓", justify="right", no_wrap=True)
    table.add_column("Final ↓", justify="right", no_wrap=True)
    table.add_column("Status", no_wrap=True)

    for i, r in enumerate(sorted_results(report.results), 1):
        latency = f"{r.latency_s * 1000:.0f} ms" if r.latency_s is not None else "-"
        scan = f"{r.scan_mbps:.1f}" if r.scan_mbps is not None else "-"
        final = f"{r.final_mbps:.1f}" if r.final_mbps is not None else "-"
        status = "final" if r.final_mbps else "ok" if r.scan_mbps else (r.error or "-")
        if r.scan_mbps and r.actual_geo:
            status += f" · geo: {r.actual_geo}"
        if r is report.winner:
            style = "green"
        elif r.final_mbps is None and r.scan_mbps is None and r.error:
            style = "red"
        else:
            style = ""
        table.add_row(
            str(i), r.candidate.provider, r.candidate.protocol, r.candidate.location,
            latency, scan, final, status, style=style,
        )
    Console(highlight=False).print(table)
