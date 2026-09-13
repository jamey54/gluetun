"""Tests for IP verification helpers, CLI redaction, and container state."""

import time
from collections.abc import Callable
from subprocess import CompletedProcess
from typing import Any

import pytest

from vpn import cli, ipinfo
from vpn.config import REAL_IP_TIMEOUT_S

PROBE_ARGS = ("docker", "exec")


def probe_result(code: int = 0, payload: str = "{}") -> CompletedProcess[str]:
    return CompletedProcess(PROBE_ARGS, code, stdout=payload if code == 0 else "", stderr="")


def run_probe(
    payloads: list[CompletedProcess[str]],
) -> tuple[Callable[..., CompletedProcess[str]], list[tuple[Any, ...]]]:
    """Stub vpn.ipinfo.run returning queued payloads; records invocations."""
    calls: list[tuple[Any, ...]] = []

    def fake_run(*args: Any, **kwargs: Any) -> CompletedProcess[str]:
        calls.append(args)
        return payloads.pop(0) if payloads else probe_result(code=1)

    return fake_run, calls


def no_sleep(_seconds: float) -> None:
    pass


@pytest.fixture(autouse=True)
def offline_real_ip(monkeypatch):
    """Host bare-IP fetch fails fast (offline stub); cache reset between tests."""
    monkeypatch.delenv("VPN_REAL_IP", raising=False)
    monkeypatch.setattr(ipinfo, "_real_ip_cache", None)
    monkeypatch.setattr(ipinfo, "_real_ip_info", None)

    def offline(url, timeout=None):
        raise OSError("offline")

    monkeypatch.setattr(ipinfo, "urlopen", offline)
    monkeypatch.setattr(time, "sleep", no_sleep)


# ---------------------------------------------------------------------------
# _log_env redaction (H2)
# ---------------------------------------------------------------------------


def test_log_env_masks_sensitive_values(monkeypatch, capsys):
    monkeypatch.setattr(cli, "DEBUG", True)
    cli._log_env(
        {
            "VPN_SERVICE_PROVIDER": "surfshark",
            "WIREGUARD_PRIVATE_KEY": "super-secret",
            "OPENVPN_PASSWORD": "hunter2",
        }
    )
    out = capsys.readouterr().out
    assert "super-secret" not in out
    assert "hunter2" not in out
    assert "VPN_SERVICE_PROVIDER=surfshark" in out
    assert "WIREGUARD_PRIVATE_KEY=***" in out
    assert "OPENVPN_PASSWORD=***" in out


def test_log_env_silent_when_debug_off(capsys):
    cli._log_env({"WIREGUARD_PRIVATE_KEY": "super-secret"})
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# real_ip
# ---------------------------------------------------------------------------


def test_real_ip_env_override(monkeypatch):
    monkeypatch.setenv("VPN_REAL_IP", "203.0.113.7")
    assert ipinfo.real_ip() == "203.0.113.7"


def test_real_ip_fetched_from_host_and_cached(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def read(self):
            return b'{"ip": "198.51.100.9", "country": "US", "city": "Newark"}'

    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append((url, timeout))
        return Resp()

    monkeypatch.setattr(ipinfo, "urlopen", fake_urlopen)
    monkeypatch.setattr(ipinfo, "_real_ip_cache", None)
    monkeypatch.setattr(ipinfo, "_real_ip_info", None)
    assert ipinfo.real_ip() == "198.51.100.9"
    assert ipinfo.real_ip() == "198.51.100.9"
    assert len(calls) == 1  # cached after first fetch
    assert calls[0][1] == REAL_IP_TIMEOUT_S


def test_real_ip_unreachable_is_none(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("no network")

    monkeypatch.setattr(ipinfo, "urlopen", boom)
    monkeypatch.setattr(ipinfo, "_real_ip_cache", None)
    monkeypatch.setattr(ipinfo, "_real_ip_info", None)
    assert ipinfo.real_ip() is None


# ---------------------------------------------------------------------------
# fetch: early stop + exclusion semantics
# ---------------------------------------------------------------------------


def test_fetch_returns_first_success_without_expectation(monkeypatch):
    fake_run, calls = run_probe([probe_result(payload='{"ip": "1.2.3.4"}')])
    monkeypatch.setattr("vpn.ipinfo.run", fake_run)
    outcome = ipinfo.fetch_ip_info(retries=3, delay=0)
    assert outcome.result is not None
    assert outcome.result.info == {"ip": "1.2.3.4"}
    assert outcome.result.matched is True  # no expectation -> trivially matched
    assert len(calls) == 1


def test_fetch_stops_at_first_non_excluded_even_if_country_differs(monkeypatch):
    # The old behavior waited for the expected country; now any non-bare IP wins.
    payloads = [
        probe_result(payload='{"ip": "9.9.9.9", "country": "NL"}'),
        probe_result(payload='{"ip": "8.8.8.8", "country": "DE"}'),
    ]
    fake_run, calls = run_probe(payloads)
    monkeypatch.setattr("vpn.ipinfo.run", fake_run)
    outcome = ipinfo.fetch_ip_info(retries=5, delay=0, expected_country="Germany")
    assert outcome.result is not None
    assert outcome.result.info["country"] == "NL"
    assert outcome.result.matched is False
    assert len(calls) == 1  # early stop


def test_fetch_matched_true_when_country_matches(monkeypatch):
    fake_run, _ = run_probe([probe_result(payload='{"ip": "8.8.8.8", "country": "DE"}')])
    monkeypatch.setattr("vpn.ipinfo.run", fake_run)
    outcome = ipinfo.fetch_ip_info(retries=3, delay=0, expected_country="Germany")
    assert outcome.result is not None and outcome.result.matched is True


def test_fetch_leak_notices_printed_each_attempt(monkeypatch, capsys):
    monkeypatch.setattr(ipinfo, "real_ip", lambda: "203.0.113.7")
    payloads = [
        probe_result(payload='{"ip": "203.0.113.7"}'),
        probe_result(payload='{"ip": "1.2.3.4"}'),
    ]
    fake_run, _ = run_probe(payloads)
    monkeypatch.setattr("vpn.ipinfo.run", fake_run)
    outcome = ipinfo.fetch_ip_info(retries=3, delay=0)
    assert outcome.result is not None
    assert capsys.readouterr().out.count("Leak: traffic not going through VPN") == 1


def test_fetch_persistent_leak_returns_none_with_last_info(monkeypatch):
    monkeypatch.setattr(ipinfo, "real_ip", lambda: "203.0.113.7")
    payloads = [probe_result(payload='{"ip": "203.0.113.7"}') for _ in range(4)]
    fake_run, calls = run_probe(payloads)
    monkeypatch.setattr("vpn.ipinfo.run", fake_run)
    outcome = ipinfo.fetch_ip_info(retries=4, delay=0)
    assert outcome.result is None
    assert outcome.last_info == {"ip": "203.0.113.7"}
    assert len(calls) == 4


def test_fetch_extra_exclude_detects_stale_route(monkeypatch, capsys):
    payloads = [
        probe_result(payload='{"ip": "9.9.9.9", "country": "JP"}'),
        probe_result(payload='{"ip": "6.6.6.6", "country": "KR"}'),
    ]
    fake_run, _ = run_probe(payloads)
    monkeypatch.setattr("vpn.ipinfo.run", fake_run)
    outcome = ipinfo.fetch_ip_info(retries=5, delay=0, exclude_ips={"9.9.9.9"})
    assert outcome.result is not None
    assert outcome.result.info["ip"] == "6.6.6.6"
    assert "Still routed via Japan (1/5)" in capsys.readouterr().out


def test_fetch_gives_up_after_retries(monkeypatch):
    fake_run, calls = run_probe([])
    monkeypatch.setattr("vpn.ipinfo.run", fake_run)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    outcome = ipinfo.fetch_ip_info(retries=4, delay=2)
    assert outcome.result is None
    assert len(calls) == 4
    assert len(sleeps) == 3  # no sleep after the final attempt


def test_fetch_first_failure_silent(monkeypatch, capsys):
    payloads = [probe_result(code=1), probe_result(payload='{"ip": "1.2.3.4"}')]
    monkeypatch.setattr("vpn.ipinfo.run", run_probe(payloads)[0])
    assert ipinfo.fetch_ip_info(retries=5, delay=0).result is not None
    assert capsys.readouterr().out == ""


def test_fetch_second_failure_prints_waiting_notice(monkeypatch, capsys):
    payloads = [
        probe_result(code=1),
        probe_result(code=1),
        probe_result(payload='{"ip": "1.2.3.4"}'),
    ]
    monkeypatch.setattr("vpn.ipinfo.run", run_probe(payloads)[0])
    ipinfo.fetch_ip_info(retries=5, delay=0)
    assert capsys.readouterr().out == "Waiting for public IP... (2/5)\n"


def test_fetch_final_failure_silent(monkeypatch, capsys):
    monkeypatch.setattr("vpn.ipinfo.run", run_probe([])[0])
    assert ipinfo.fetch_ip_info(retries=4, delay=0).result is None
    assert capsys.readouterr().out == (
        "Waiting for public IP... (2/4)\nWaiting for public IP... (3/4)\n"
    )


def test_fetch_invalid_json_retried(monkeypatch):
    payloads = [
        probe_result(payload="<html>gateway error</html>"),
        probe_result(payload='{"country": "FR", "ip": "2.2.2.2"}'),
    ]
    fake_run, calls = run_probe(payloads)
    monkeypatch.setattr("vpn.ipinfo.run", fake_run)
    outcome = ipinfo.fetch_ip_info(retries=5, delay=0, expected_country="France")
    assert outcome.result is not None
    assert outcome.result.info["country"] == "FR"
    assert len(calls) == 2


def test_same_country_used_for_verification():
    assert ipinfo._same_country("de", "Germany")
    assert not ipinfo._same_country("", "Germany")


# ---------------------------------------------------------------------------
# container state helpers (M3)
# ---------------------------------------------------------------------------


def test_container_running_requires_running_state(monkeypatch):
    states = iter(["exited", "running", None])
    monkeypatch.setattr("vpn.docker.container_status", lambda name=None: next(states))
    from vpn.docker import container_running

    assert container_running() is False  # exited
    assert container_running() is True  # running
    assert container_running() is False  # missing container
