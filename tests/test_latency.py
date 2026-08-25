"""Tests for host-side TCP latency probes."""

from typing import Any

from vpn import latency


class FakeSocket:
    def __enter__(self) -> "FakeSocket":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def test_probe_host_returns_elapsed_seconds(monkeypatch):
    calls: list[Any] = []

    def connect(address, timeout=None):
        calls.append((address, timeout))
        return FakeSocket()

    monkeypatch.setattr("vpn.latency.socket.create_connection", connect)
    result = latency.probe_host("example.com")
    assert isinstance(result, float)
    assert result >= 0
    assert calls == [(("example.com", 443), 2.0)]


def test_probe_host_unreachable_is_none(monkeypatch):
    def connect(address, timeout=None):
        raise ConnectionRefusedError()

    monkeypatch.setattr("vpn.latency.socket.create_connection", connect)
    assert latency.probe_host("down.example.com") is None


def test_probe_hosts_maps_results_and_dedupes(monkeypatch):
    delays = {"a.example.com": 0.1, "b.example.com": None}
    seen: list[str] = []

    def connect(address, timeout=None):
        host = address[0]
        seen.append(host)
        if delays[host] is None:
            raise OSError("nope")
        return FakeSocket()

    monkeypatch.setattr("vpn.latency.socket.create_connection", connect)
    result = latency.probe_hosts(["b.example.com", "a.example.com", "a.example.com"], timeout=1.5)
    assert sorted(seen) == ["a.example.com", "b.example.com"]  # probed once each
    assert set(result) == {"a.example.com", "b.example.com"}
    assert result["b.example.com"] is None
    assert isinstance(result["a.example.com"], float)


def test_probe_hosts_empty_input():
    assert latency.probe_hosts([]) == {}
