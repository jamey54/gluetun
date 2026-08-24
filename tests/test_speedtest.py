"""Tests for speed measurement helpers and CLI country comparison."""

from subprocess import CompletedProcess

from vpn import speedtest
from vpn.ipinfo import _same_country
from vpn.speedtest import DEFAULT_SIZE_MB, mbps


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
