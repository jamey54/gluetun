"""Tests for reading the running container's configuration back."""

import pytest

from vpn import config, docker
from vpn.docker import CurrentVpn


@pytest.fixture()
def inspect_env(monkeypatch):
    def set_env(lines):
        monkeypatch.setattr(docker, "inspect_container", lambda fmt: "\n".join(lines))

    return set_env


def test_get_current_vpn_full(inspect_env):
    inspect_env(
        [
            "PATH=/usr/bin",
            "VPN_SERVICE_PROVIDER=surfshark",
            "VPN_TYPE=openvpn",
            "SERVER_COUNTRIES=Germany",
            "SERVER_CITIES=Berlin",
        ]
    )
    current = docker.get_current_vpn()
    assert current == CurrentVpn("surfshark", "openvpn", "Germany", "Berlin")


def test_get_current_vpn_missing_container():
    monkey = pytest.MonkeyPatch()
    monkey.setattr(docker, "inspect_container", lambda fmt: None)
    assert docker.get_current_vpn() is None
    monkey.undo()


def test_get_current_vpn_no_provider(inspect_env):
    inspect_env(["VPN_TYPE=wireguard"])
    assert docker.get_current_vpn() is None


def test_empty_values_become_none(inspect_env):
    # compose always passes SERVER_* through; empty means 'unconstrained'
    inspect_env(
        [
            "VPN_SERVICE_PROVIDER=protonvpn",
            "VPN_TYPE=wireguard",
            "SERVER_COUNTRIES=",
            "SERVER_CITIES=",
        ]
    )
    current = docker.get_current_vpn()
    assert current is not None
    assert current.countries is None
    assert current.cities is None


def test_location_overrides_omit_unset():
    full = CurrentVpn("p", "wireguard", countries="Germany", cities="Berlin")
    assert full.location_overrides() == {"SERVER_COUNTRIES": "Germany", "SERVER_CITIES": "Berlin"}
    bare = CurrentVpn("p", "wireguard")
    assert bare.location_overrides() == {}


# ---------------------------------------------------------------------------
# compose invocation
# ---------------------------------------------------------------------------


def test_compose_merges_env_without_tempfile(monkeypatch):
    from subprocess import CompletedProcess

    seen_args: tuple[str, ...] = ()
    seen_env: dict[str, str] | None = None

    def fake_run(*args, capture=False, check=True, env=None):
        nonlocal seen_args, seen_env
        seen_args, seen_env = args, env
        return CompletedProcess(args, 0)

    monkeypatch.setattr(docker, "run", fake_run)
    docker.compose("up", "-d", env_overrides={"WIREGUARD_PRIVATE_KEY": "secret"})

    assert seen_args[:4] == ("docker", "compose", "-f", config.COMPOSE_FILE)
    assert seen_env is not None
    assert seen_env["WIREGUARD_PRIVATE_KEY"] == "secret"
    assert seen_env["PATH"]  # process env preserved
