"""Tests for the provider registry and credential mapping."""

import pytest

from vpn import providers
from vpn.providers import (
    DEFAULT_PROTOCOL,
    active_protocols,
    get_active_providers,
    get_provider_env,
    validate_provider,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        "SURFSHARK_WIREGUARD_PRIVATE_KEY",
        "SURFSHARK_WIREGUARD_ADDRESSES",
        "SURFSHARK_OPENVPN_USER",
        "SURFSHARK_OPENVPN_PASSWORD",
        "PROTONVPN_WIREGUARD_PRIVATE_KEY",
        "PROTONVPN_WIREGUARD_ADDRESSES",
        "PROTONVPN_OPENVPN_USER",
        "PROTONVPN_OPENVPN_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)


def test_active_protocols_requires_all_vars(monkeypatch):
    assert active_protocols("surfshark") == []
    monkeypatch.setenv("SURFSHARK_OPENVPN_USER", "u")
    assert active_protocols("surfshark") == []  # password still missing
    monkeypatch.setenv("SURFSHARK_OPENVPN_PASSWORD", "p")
    assert active_protocols("surfshark") == ["openvpn"]


def test_get_active_providers_pairs(monkeypatch):
    monkeypatch.setenv("PROTONVPN_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("PROTONVPN_WIREGUARD_ADDRESSES", "10.2.0.2/32")
    assert get_active_providers() == {("protonvpn", "wireguard")}


def test_validate_provider_normalizes_name(monkeypatch):
    monkeypatch.setenv("SURFSHARK_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("SURFSHARK_WIREGUARD_ADDRESSES", "a")
    name, protocol = validate_provider("SurfShark")
    assert (name, protocol) == ("surfshark", DEFAULT_PROTOCOL)


def test_validate_provider_unknown_exits():
    with pytest.raises(SystemExit, match="Unknown provider"):
        validate_provider("nordvpn")


def test_validate_provider_unknown_protocol_exits():
    with pytest.raises(SystemExit, match="Unknown protocol"):
        validate_provider("surfshark", "tls-crypt")


def test_validate_provider_missing_env_hint(monkeypatch):
    monkeypatch.setenv("PROTONVPN_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("PROTONVPN_OPENVPN_USER", "u")
    monkeypatch.setenv("PROTONVPN_OPENVPN_PASSWORD", "p")
    with pytest.raises(SystemExit) as excinfo:
        validate_provider("protonvpn", "wireguard")  # ADDRESSES required but missing
    # openvpn has full credentials -> suggests switching
    assert "--protocol openvpn" in str(excinfo.value)


def test_validate_provider_missing_env_no_hint(monkeypatch):
    monkeypatch.setenv("PROTONVPN_WIREGUARD_PRIVATE_KEY", "k")
    with pytest.raises(SystemExit) as excinfo:
        validate_provider("protonvpn", "wireguard")  # ADDRESSES missing, no alternative
    assert "--protocol" not in str(excinfo.value)


def test_validate_provider_surfshark_key_alone_suffices(monkeypatch):
    # surfshark treats WIREGUARD_ADDRESSES as optional
    monkeypatch.setenv("SURFSHARK_WIREGUARD_PRIVATE_KEY", "k")
    assert validate_provider("surfshark") == ("surfshark", DEFAULT_PROTOCOL)


def test_validate_provider_success_with_creds(monkeypatch):
    monkeypatch.setenv("SURFSHARK_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("SURFSHARK_WIREGUARD_ADDRESSES", "10.0.0.1/32")
    assert validate_provider("surfshark", "wireguard") == ("surfshark", "wireguard")


def test_get_provider_env_maps_credentials(monkeypatch):
    monkeypatch.setenv("PROTONVPN_WIREGUARD_PRIVATE_KEY", "secret-key")
    monkeypatch.setenv("PROTONVPN_WIREGUARD_ADDRESSES", "10.2.0.2/32")
    env = get_provider_env("protonvpn", "wireguard")
    assert env["WIREGUARD_PRIVATE_KEY"] == "secret-key"
    assert env["WIREGUARD_ADDRESSES"] == "10.2.0.2/32"
    assert env["VPN_SERVICE_PROVIDER"] == "protonvpn"
    assert env["VPN_TYPE"] == "wireguard"
    assert "OPENVPN_USER" not in env


def test_get_provider_env_skips_unset():
    env = get_provider_env("protonvpn", "wireguard")
    assert "WIREGUARD_PRIVATE_KEY" not in env


def test_registry_shapes_consistent():
    """Every required credential must be mapped to a gluetun variable."""
    for provider, protocols in providers.PROVIDERS.items():
        assert protocols, provider
        for protocol, config in protocols.items():
            unmapped = set(config.required_env) - set(config.env_map.values())
            assert not unmapped, f"{provider}/{protocol}: {unmapped}"
