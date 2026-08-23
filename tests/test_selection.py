"""Tests for up/update preserving the running container's selection (H1)."""

from subprocess import CompletedProcess

import pytest
from click.testing import CliRunner

from vpn import cli, docker
from vpn.speedtest import DEFAULT_SIZE_MB


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("SURFSHARK_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("PROTONVPN_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setattr(cli, "print_ip_status", lambda expected_country=None: True)
    monkeypatch.setattr(cli, "measure", lambda size=DEFAULT_SIZE_MB: None)


@pytest.fixture()
def compose_calls(monkeypatch):
    calls = []

    def fake_compose(*args, env_overrides=None):
        calls.append((args, env_overrides))
        return CompletedProcess((), 0)

    monkeypatch.setattr(cli, "compose", fake_compose)
    return calls


def invoke(args):
    return CliRunner().invoke(cli.main, args, catch_exceptions=False)


def test_up_carries_location_and_protocol(monkeypatch, compose_calls):
    monkeypatch.setenv("SURFSHARK_OPENVPN_USER", "u")
    monkeypatch.setenv("SURFSHARK_OPENVPN_PASSWORD", "p")
    current = docker.CurrentVpn(
        provider="surfshark", protocol="openvpn", countries="Germany", cities="Berlin"
    )
    monkeypatch.setattr(cli, "get_current_vpn", lambda: current)
    result = invoke(["up", "--provider", "surfshark"])
    assert result.exit_code == 0
    _, overrides = compose_calls[0]
    assert overrides["SERVER_COUNTRIES"] == "Germany"
    assert overrides["SERVER_CITIES"] == "Berlin"
    assert overrides["VPN_TYPE"] == "openvpn"


def test_up_protocol_defaults_to_running_not_wireguard(monkeypatch, compose_calls):
    monkeypatch.setenv("SURFSHARK_OPENVPN_USER", "u")
    monkeypatch.setenv("SURFSHARK_OPENVPN_PASSWORD", "p")
    current = docker.CurrentVpn(provider="surfshark", protocol="openvpn")
    monkeypatch.setattr(cli, "get_current_vpn", lambda: current)
    invoke(["up", "--provider", "surfshark"])
    _, overrides = compose_calls[0]
    assert overrides["VPN_TYPE"] == "openvpn"
    assert "SERVER_COUNTRIES" not in overrides  # nothing to preserve


def test_up_fresh_install_no_location_no_container(monkeypatch, compose_calls):
    monkeypatch.setattr(cli, "get_current_vpn", lambda: None)
    invoke(["up", "--provider", "surfshark"])
    _, overrides = compose_calls[0]
    assert overrides["VPN_TYPE"] == "wireguard"  # DEFAULT_PROTOCOL fallback
    assert "SERVER_COUNTRIES" not in overrides
    assert "SERVER_CITIES" not in overrides


def test_update_recreates_with_full_selection(monkeypatch, compose_calls):
    current = docker.CurrentVpn(provider="protonvpn", protocol="wireguard", countries="Netherlands")
    monkeypatch.setattr(cli, "get_current_vpn", lambda: current)
    pulls = []
    monkeypatch.setattr(cli, "run", lambda *a, **kw: pulls.append(a) or CompletedProcess((), 0))
    result = invoke(["update"])
    assert result.exit_code == 0
    args, overrides = compose_calls[0]
    assert "--force-recreate" in args
    assert overrides["SERVER_COUNTRIES"] == "Netherlands"
    assert overrides["WIREGUARD_PRIVATE_KEY"] == "k"
    assert any("pull" in c for c in pulls)


def test_update_without_container_exits(monkeypatch, compose_calls):
    monkeypatch.setattr(cli, "get_current_vpn", lambda: None)
    result = invoke(["update"])
    assert result.exit_code != 0
    assert "No running container" in result.output
    assert compose_calls == []


def test_up_preserved_protocol_without_creds_falls_back(monkeypatch, compose_calls):
    # container runs openvpn but OpenVPN creds are gone -> fall back to wireguard
    current = docker.CurrentVpn(provider="surfshark", protocol="openvpn")
    monkeypatch.setattr(cli, "get_current_vpn", lambda: current)
    result = invoke(["up", "--provider", "surfshark"])
    assert result.exit_code == 0
    _, overrides = compose_calls[0]
    assert overrides["VPN_TYPE"] == "wireguard"


def test_explicit_protocol_requires_its_own_creds(monkeypatch, compose_calls):
    current = docker.CurrentVpn(provider="surfshark", protocol="wireguard")
    monkeypatch.setattr(cli, "get_current_vpn", lambda: current)
    result = invoke(["up", "--provider", "surfshark", "--protocol", "openvpn"])
    assert result.exit_code != 0
    assert "Missing env vars" in result.output
    assert compose_calls == []  # never started
