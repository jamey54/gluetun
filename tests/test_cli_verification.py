"""Tests for CLI IP verification and debug redaction."""

import time
from collections.abc import Callable
from subprocess import CompletedProcess
from typing import Any

from vpn import cli

PROBE_ARGS = ("docker", "exec")


def probe_result(code: int = 0, payload: str = "{}") -> CompletedProcess[str]:
    return CompletedProcess(PROBE_ARGS, code, stdout=payload if code == 0 else "", stderr="")


def run_probe(
    payloads: list[CompletedProcess[str]],
) -> tuple[Callable[..., CompletedProcess[str]], list[tuple[Any, ...]]]:
    """Stub vpn.cli.run returning queued payloads; records invocations."""
    calls: list[tuple[Any, ...]] = []

    def fake_run(*args: Any, **kwargs: Any) -> CompletedProcess[str]:
        calls.append(args)
        return payloads.pop(0) if payloads else probe_result(code=1)

    return fake_run, calls


def no_sleep(_seconds: float) -> None:
    pass


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


def test_fetch_returns_first_success_without_expectation(monkeypatch):
    fake_run, calls = run_probe([probe_result(payload='{"ip": "1.2.3.4"}')])
    monkeypatch.setattr("vpn.cli.run", fake_run)
    monkeypatch.setattr(time, "sleep", no_sleep)
    info = cli.fetch_ip_info(retries=3, delay=0)
    assert info == {"ip": "1.2.3.4"}
    assert len(calls) == 1


def test_fetch_retries_until_country_matches(monkeypatch):
    payloads = [
        probe_result(payload='{"country": "NL", "city": "Amsterdam"}'),
        probe_result(payload='{"country": "US", "city": "Boston"}'),
        probe_result(payload='{"country": "DE", "city": "Berlin"}'),
    ]
    fake_run, calls = run_probe(payloads)
    monkeypatch.setattr("vpn.cli.run", fake_run)
    monkeypatch.setattr(time, "sleep", no_sleep)
    info = cli.fetch_ip_info(retries=5, delay=0, expected_country="Germany")
    assert info is not None
    assert info["country"] == "DE"
    assert len(calls) == 3


def test_fetch_city_flip_does_not_count_as_success(monkeypatch):
    # geoIP city changing must NOT be treated as connected when country mismatches
    payloads = [
        probe_result(payload='{"country": "US", "city": ""}'),
        probe_result(payload='{"country": "US", "city": "Boston"}'),
    ]
    fake_run, _ = run_probe(payloads)
    monkeypatch.setattr("vpn.cli.run", fake_run)
    monkeypatch.setattr(time, "sleep", no_sleep)
    assert cli.fetch_ip_info(retries=5, delay=0, expected_country="Germany") is None


def test_fetch_gives_up_after_retries(monkeypatch):
    fake_run, calls = run_probe([])
    monkeypatch.setattr("vpn.cli.run", fake_run)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    assert cli.fetch_ip_info(retries=4, delay=2) is None
    assert len(calls) == 4
    assert len(sleeps) == 3  # no sleep after the final attempt


def test_fetch_invalid_json_retried(monkeypatch):
    payloads = [
        probe_result(payload="<html>gateway error</html>"),
        probe_result(payload='{"country": "FR"}'),
    ]
    fake_run, calls = run_probe(payloads)
    monkeypatch.setattr("vpn.cli.run", fake_run)
    monkeypatch.setattr(time, "sleep", no_sleep)
    info = cli.fetch_ip_info(retries=5, delay=0, expected_country="France")
    assert info == {"country": "FR"}
    assert len(calls) == 2


def test_same_country_used_for_verification():
    assert cli._same_country("de", "Germany")
    assert not cli._same_country("", "Germany")


# ---------------------------------------------------------------------------
# container state helpers (M3)
# ---------------------------------------------------------------------------


def test_container_running_requires_running_state(monkeypatch):
    states = iter(["exited", "running", None])
    monkeypatch.setattr("vpn.docker.container_status", lambda: next(states))
    from vpn.docker import container_running

    assert container_running() is False  # exited
    assert container_running() is True  # running
    assert container_running() is False  # missing container
