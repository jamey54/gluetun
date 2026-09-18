"""Tests for speed measurement helpers and CLI country comparison."""

from subprocess import CompletedProcess

from vpn import speedtest
from vpn.config import DEFAULT_SIZE_MB
from vpn.ipinfo import _same_country
from vpn.speedtest import mbps


def test_mbps():
    # 25,000,000 bytes in 10 s -> 20 Mbit/s
    assert mbps(25_000_000, 10) == 20
    assert mbps(1_000_000, 1) == 8


def test_default_size():
    assert DEFAULT_SIZE_MB == 25


def test_measure_computes_throughput(monkeypatch):
    times = iter([100.0, 110.0])  # 10 s elapsed
    monkeypatch.setattr("time.monotonic", lambda: next(times))

    def fake_run(*args, **kwargs):
        return CompletedProcess(args, 0)

    monkeypatch.setattr(speedtest, "run", fake_run)
    result = speedtest.measure(size_mb=25)
    assert result is not None
    assert abs(result["mbits"] - 20.0) < 1e-9
    assert result["seconds"] == 10.0
    assert result["mbytes"] == 25.0


def test_measure_failure_returns_none(monkeypatch):
    def fake_run(*args, **kwargs):
        return CompletedProcess(args, 1)

    monkeypatch.setattr(speedtest, "run", fake_run)
    assert speedtest.measure() is None


def test_measure_bounds_the_docker_exec(monkeypatch):
    """A stalled docker exec must time out rather than hang the speed test."""
    times = iter([100.0, 101.0])
    monkeypatch.setattr("time.monotonic", lambda: next(times))
    seen: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return CompletedProcess(args, 0)

    monkeypatch.setattr(speedtest, "run", fake_run)
    speedtest.measure(size_mb=25, timeout=120)
    assert seen["timeout"] == 130  # inner wget bound + exec overhead buffer


def test_measure_timeout_is_failure(monkeypatch):
    monkeypatch.setattr("time.monotonic", lambda: 100.0)

    def fake_run(*args, **kwargs):
        return CompletedProcess(args, 124, stdout="", stderr="timed out")

    monkeypatch.setattr(speedtest, "run", fake_run)
    assert speedtest.measure() is None


def test_same_country_code_vs_name():
    assert _same_country("DE", "Germany")
    assert _same_country("United States", "US")


def test_same_country_accent_insensitive():
    assert _same_country("Curaçao", "Curacao")


def test_same_country_mismatch():
    assert not _same_country("DE", "FR")


def test_same_country_empty_strings():
    # empty vs empty must not count as a match
    assert not _same_country("", "")
