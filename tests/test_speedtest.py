"""Tests for speed measurement helpers and CLI country comparison."""

import pytest

from epoxy import speedtest
from epoxy.config import DEFAULT_SIZE_MB
from epoxy.ipinfo import _same_country
from epoxy.speedtest import Result, format_result, mbps


def fake_stream(chunks=(), returncode=0, seen=None):
    """A run_streamed stand-in that replays chunk sizes and records its call.

    Keyword arguments land in `seen` so a test can assert on the argv the
    download was started with, or on the timeout it was bounded by.
    """
    seen = {} if seen is None else seen

    def _run(*args, on_chunk, **kwargs):
        seen["args"] = args
        seen["on_chunk"] = on_chunk
        seen.update(kwargs)
        for size in chunks:
            on_chunk(size)
        return returncode

    return _run


def test_mbps():
    # 25,000,000 bytes in 10 s -> 20 Mbit/s
    assert mbps(25_000_000, 10) == 20
    assert mbps(1_000_000, 1) == 8


def test_default_size():
    assert DEFAULT_SIZE_MB == 25


def test_measure_computes_throughput(monkeypatch):
    times = iter([100.0, 110.0])  # 10 s elapsed
    monkeypatch.setattr("time.monotonic", lambda: next(times))
    monkeypatch.setattr(speedtest, "run_streamed", fake_stream())
    result = speedtest.measure(size_mb=25)
    assert result is not None
    assert abs(result.mbits - 20.0) < 1e-9
    assert result.seconds == 10.0
    assert result.mbytes == 25.0


def test_measure_streams_the_payload_to_our_pipe(monkeypatch):
    """-O- is what makes progress real: the bytes reach us instead of /dev/null."""
    seen: dict[str, object] = {}
    monkeypatch.setattr("time.monotonic", lambda: 100.0)
    monkeypatch.setattr(speedtest, "run_streamed", fake_stream(seen=seen))
    speedtest.measure(size_mb=25, container="testbox")
    args = seen["args"]
    assert isinstance(args, tuple)
    assert args[:3] == ("docker", "exec", "testbox")
    # The same single-token spelling ipinfo.probe_provider already relies on.
    assert "-O-" in args
    assert "/dev/null" not in args
    assert args[-1] == "https://speed.cloudflare.com/__down?bytes=25000000"
    assert callable(seen["on_chunk"])  # chunks are counted, not discarded blind


def test_measure_reports_progress_as_bytes_arrive(monkeypatch):
    """The callback sees running totals, so a bar can advance on partial reads."""
    seen_progress: list[tuple[int, int]] = []
    monkeypatch.setattr("time.monotonic", lambda: 100.0)
    monkeypatch.setattr(speedtest, "run_streamed", fake_stream(chunks=[7, 3, 40]))
    speedtest.measure(
        size_mb=25, on_progress=lambda done, total: seen_progress.append((done, total))
    )
    assert seen_progress == [(7, 25_000_000), (10, 25_000_000), (50, 25_000_000)]


def test_measure_without_a_callback_stays_silent(monkeypatch):
    """bench passes no callback, so the download must not try to draw anything."""
    monkeypatch.setattr("time.monotonic", lambda: 100.0)
    monkeypatch.setattr(speedtest, "run_streamed", fake_stream(chunks=[100, 200]))
    assert speedtest.measure(size_mb=25) is not None


def test_measure_failure_returns_none(monkeypatch):
    monkeypatch.setattr("time.monotonic", lambda: 100.0)
    monkeypatch.setattr(speedtest, "run_streamed", fake_stream(returncode=1))
    assert speedtest.measure() is None


def test_result_is_a_frozen_record():
    """Attributes, not string keys, and no in-place mutation of a shared record."""
    r = Result(mbits=20.0, seconds=10.0, mbytes=25.0)
    assert (r.mbits, r.seconds, r.mbytes) == (20.0, 10.0, 25.0)
    with pytest.raises((AttributeError, TypeError)):
        r.mbits = 1.0  # type: ignore[misc]
    with pytest.raises(TypeError):
        r["mbits"] = 1.0  # type: ignore[index]
    assert r == Result(mbits=20.0, seconds=10.0, mbytes=25.0)  # value semantics


def test_format_result_renders_the_record():
    text = format_result(Result(mbits=20.0, seconds=10.0, mbytes=25.0))
    assert text == "↓ 20.0 Mbit/s (25 MB in 10.0s)"


def test_measure_bounds_the_docker_exec(monkeypatch):
    """A stalled docker exec must time out rather than hang the speed test."""
    seen: dict[str, object] = {}
    monkeypatch.setattr("time.monotonic", lambda: 100.0)
    monkeypatch.setattr(speedtest, "run_streamed", fake_stream(seen=seen))
    speedtest.measure(size_mb=25, timeout=120)
    assert seen["timeout"] == 130  # inner wget bound + exec overhead buffer


def test_measure_timeout_is_failure(monkeypatch):
    monkeypatch.setattr("time.monotonic", lambda: 100.0)
    monkeypatch.setattr(speedtest, "run_streamed", fake_stream(returncode=124))
    assert speedtest.measure() is None


def test_measure_instant_exec_never_divide_by_zero(monkeypatch):
    times = iter([100.0, 100.0])  # zero elapsed time
    monkeypatch.setattr("time.monotonic", lambda: next(times))
    monkeypatch.setattr(speedtest, "run_streamed", fake_stream())
    result = speedtest.measure(size_mb=25)
    assert result is not None
    assert result.seconds > 0  # clamped, not a divide-by-zero crash
    assert result.mbits > 0 and result.mbits != float("inf")


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
