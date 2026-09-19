"""Tests for IP verification helpers, CLI redaction, and container state."""

import time
from collections.abc import Callable

import pytest

from vpn import cli, ipinfo


def stub_probe(
    results: list[dict[str, object] | None],
    sources: tuple[str, ...] = ("ipinfo",),
) -> tuple[Callable[..., ipinfo._Probe | None], list[str]]:
    """Stub vpn.ipinfo._probe with queued observations; records poll count."""
    calls: list[str] = []

    def fake_probe(container: str | None = None) -> ipinfo._Probe | None:
        calls.append(container or "")
        result = results.pop(0) if results else None
        if result is None:
            return None
        return ipinfo._Probe(info=result, sources=sources)

    return fake_probe, calls


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
    monkeypatch.setattr(
        ipinfo,
        "urlopen",
        lambda *a, **k: _FakeResponse('{"ip": "198.51.100.9", "country": "GB"}'),
    )

    monkeypatch.setattr(ipinfo, "_real_ip_cache", None)
    monkeypatch.setattr(ipinfo, "_real_ip_info", None)
    assert ipinfo.real_ip() == "198.51.100.9"
    assert ipinfo.real_ip() == "198.51.100.9"
    info = ipinfo.real_ip_info()
    assert info is not None and info["ip"] == "198.51.100.9"


def test_real_ip_unreachable_is_none(monkeypatch):
    monkeypatch.setattr(ipinfo, "_real_ip_cache", None)
    monkeypatch.setattr(ipinfo, "_real_ip_info", None)
    assert ipinfo.real_ip() is None


# ---------------------------------------------------------------------------
# fetch: early stop + exclusion semantics
# ---------------------------------------------------------------------------


def test_fetch_returns_first_success_without_expectation(monkeypatch):
    probe, calls = stub_probe([{"ip": "1.2.3.4"}])
    monkeypatch.setattr(ipinfo, "_probe", probe)
    outcome = ipinfo.fetch_ip_info(retries=3, delay=0)
    assert outcome.result is not None
    assert outcome.result.info == {"ip": "1.2.3.4"}
    assert outcome.result.matched is True  # no expectation -> trivially matched
    assert len(calls) == 1


def test_fetch_stops_at_first_non_excluded_even_if_country_differs(monkeypatch):
    """The old behavior waited for the expected country; now any non-bare IP wins."""
    probe, calls = stub_probe([{"ip": "9.9.9.9", "country": "NL"}])
    monkeypatch.setattr(ipinfo, "_probe", probe)
    outcome = ipinfo.fetch_ip_info(retries=5, delay=0, expected_country="Germany")
    assert outcome.result is not None
    assert outcome.result.info["country"] == "NL"
    assert outcome.result.matched is False
    assert len(calls) == 1  # early stop


def test_fetch_matched_true_when_country_matches(monkeypatch):
    probe, _ = stub_probe([{"ip": "8.8.8.8", "country": "DE"}])
    monkeypatch.setattr(ipinfo, "_probe", probe)
    outcome = ipinfo.fetch_ip_info(retries=3, delay=0, expected_country="Germany")
    assert outcome.result is not None and outcome.result.matched is True


def test_fetch_preserves_resilient_sources(monkeypatch):
    probe, _ = stub_probe([{"ip": "5.6.7.8"}], sources=("cloudflare", "ifconfigco"))
    monkeypatch.setattr(ipinfo, "_probe", probe)
    outcome = ipinfo.fetch_ip_info(retries=1, delay=0)
    assert outcome.result is not None
    assert outcome.result.sources == ("cloudflare", "ifconfigco")


def test_fetch_leak_notices_printed_each_attempt(monkeypatch, capsys):
    monkeypatch.setattr(ipinfo, "real_ip", lambda: "203.0.113.7")
    probe, _ = stub_probe([{"ip": "203.0.113.7"}, {"ip": "1.2.3.4"}])
    monkeypatch.setattr(ipinfo, "_probe", probe)
    outcome = ipinfo.fetch_ip_info(retries=3, delay=0)
    assert outcome.result is not None
    assert capsys.readouterr().out.count("Leak: traffic not going through VPN") == 1


def test_fetch_persistent_leak_returns_none_with_last_info(monkeypatch):
    monkeypatch.setattr(ipinfo, "real_ip", lambda: "203.0.113.7")
    probe, calls = stub_probe([{"ip": "203.0.113.7"} for _ in range(4)])
    monkeypatch.setattr(ipinfo, "_probe", probe)
    outcome = ipinfo.fetch_ip_info(retries=4, delay=0)
    assert outcome.result is None
    assert outcome.last_info == {"ip": "203.0.113.7"}
    assert len(calls) == 4


def test_fetch_extra_exclude_detects_stale_route(monkeypatch, capsys):
    probe, _ = stub_probe([{"ip": "9.9.9.9", "country": "JP"}, {"ip": "6.6.6.6", "country": "KR"}])
    monkeypatch.setattr(ipinfo, "_probe", probe)
    outcome = ipinfo.fetch_ip_info(retries=5, delay=0, exclude_ips={"9.9.9.9"})
    assert outcome.result is not None
    assert outcome.result.info["ip"] == "6.6.6.6"
    assert "Still routed via Japan (1/5)" in capsys.readouterr().out


def test_fetch_gives_up_after_retries(monkeypatch):
    probe, calls = stub_probe([])
    monkeypatch.setattr(ipinfo, "_probe", probe)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    outcome = ipinfo.fetch_ip_info(retries=4, delay=2)
    assert outcome.result is None
    assert len(calls) == 4
    assert len(sleeps) == 3  # no sleep after the final attempt


def test_fetch_first_failure_silent(monkeypatch, capsys):
    probe, _ = stub_probe([None, {"ip": "1.2.3.4"}])
    monkeypatch.setattr(ipinfo, "_probe", probe)
    assert ipinfo.fetch_ip_info(retries=5, delay=0).result is not None
    assert capsys.readouterr().out == ""


def test_fetch_second_failure_prints_waiting_notice(monkeypatch, capsys):
    probe, _ = stub_probe([None, None, {"ip": "1.2.3.4"}])
    monkeypatch.setattr(ipinfo, "_probe", probe)
    ipinfo.fetch_ip_info(retries=5, delay=0)
    assert capsys.readouterr().out == "Waiting for public IP... (2/5)\n"


def test_fetch_final_failure_silent(monkeypatch, capsys):
    probe, _ = stub_probe([])
    monkeypatch.setattr(ipinfo, "_probe", probe)
    assert ipinfo.fetch_ip_info(retries=4, delay=0).result is None
    assert capsys.readouterr().out == (
        "Waiting for public IP... (2/4)\nWaiting for public IP... (3/4)\n"
    )


def test_fetch_rejects_body_without_ip(monkeypatch):
    """A service error body (no `ip`) must not count as a verification."""
    probe, calls = stub_probe(
        [{"status": 429, "error": {"title": "Rate limit hit"}}, {"ip": "2.2.2.2"}]
    )
    monkeypatch.setattr(ipinfo, "_probe", probe)
    outcome = ipinfo.fetch_ip_info(retries=5, delay=0)
    assert outcome.result is not None
    assert outcome.result.info["ip"] == "2.2.2.2"
    assert len(calls) == 2  # first poll rejected, second accepted


def test_same_country_used_for_verification():
    assert ipinfo._same_country("de", "Germany")
    assert not ipinfo._same_country("", "Germany")


# ---------------------------------------------------------------------------
# print_ip_status: resilient-source notes + verdict
# ---------------------------------------------------------------------------


def test_print_ip_status_fail_closed_when_bare_unknown_from_backup_sources(
    monkeypatch, capsys
):
    """Backup echo sources work, but with no country match and an unknown host
    bare IP the verdict is fail-closed: never a silent green (C2)."""
    probe = ipinfo._Probe({"ip": "5.6.7.8"}, sources=("cloudflare",))
    monkeypatch.setattr(ipinfo, "_probe", lambda container=None: probe)
    assert ipinfo.print_ip_status() is False
    out = capsys.readouterr().out
    assert "Public IP confirmed via cloudflare" in out
    assert "ipinfo.io was rate-limited or unreachable" in out
    assert "Cannot verify: could not determine the host's bare IP (fail-closed)" in out


def test_print_ip_status_no_note_when_ipinfo_served(monkeypatch, capsys):
    probe = ipinfo._Probe({"ip": "1.2.3.4", "country": "US"}, sources=("ipinfo",))
    monkeypatch.setattr(ipinfo, "_probe", lambda container=None: probe)
    assert ipinfo.print_ip_status() is False  # bare IP unknown -> fail-closed
    assert "Public IP confirmed via" not in capsys.readouterr().out


def test_print_ip_status_still_detects_leak_from_backup_sources(monkeypatch):
    monkeypatch.setattr(ipinfo, "real_ip", lambda: "203.0.113.7")
    probe = ipinfo._Probe({"ip": "203.0.113.7"}, sources=("cloudflare",))
    monkeypatch.setattr(ipinfo, "_probe", lambda container=None: probe)
    assert ipinfo.print_ip_status() is False


# ---------------------------------------------------------------------------
# current_exit_ip: single-shot read used by up/connect/bench exclusions
# ---------------------------------------------------------------------------


def test_current_exit_ip_returns_accepted_observation(monkeypatch):
    probe, _ = stub_probe([{"ip": "9.9.9.9", "country": "DE"}])
    monkeypatch.setenv("VPN_REAL_IP", "1.1.1.1")
    monkeypatch.setattr(ipinfo, "_probe", probe)
    assert ipinfo.current_exit_ip() == "9.9.9.9"


def test_current_exit_ip_falls_back_to_last_observation(monkeypatch):
    """When every observation is excluded (e.g. still on the bare IP), the
    single-shot still reports what was last seen rather than None."""
    probe, _ = stub_probe([{"ip": "1.1.1.1", "country": "Egypt"}])
    monkeypatch.setenv("VPN_REAL_IP", "1.1.1.1")
    monkeypatch.setattr(ipinfo, "_probe", probe)
    assert ipinfo.current_exit_ip() == "1.1.1.1"


def test_current_exit_ip_none_when_probe_failed(monkeypatch):
    probe, _ = stub_probe([None])
    monkeypatch.setattr(ipinfo, "_probe", probe)
    assert ipinfo.current_exit_ip() is None


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


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self) -> bytes:
        return self._body.encode()
