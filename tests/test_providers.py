"""Tests for the provider registry and credential mapping."""

import pytest

from epoxy import providers
from epoxy.config import DEFAULT_PROTOCOL
from epoxy.providers import (
    PROVIDERS,
    active_protocols,
    choose_protocol,
    get_active_providers,
    get_provider_env,
    resolve_provider,
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


def test_get_provider_env_accepts_explicit_env():
    """An explicit snapshot wins over the instance env (parallel bench workers)."""
    env = get_provider_env(
        "surfshark",
        "wireguard",
        {"SURFSHARK_WIREGUARD_PRIVATE_KEY": "k", "SURFSHARK_WIREGUARD_ADDRESSES": "a/16"},
    )
    assert env["WIREGUARD_PRIVATE_KEY"] == "k"
    assert env["WIREGUARD_ADDRESSES"] == "a/16"


def test_registry_shapes_consistent():
    """Every required credential must be mapped to a container variable."""
    for provider, protocols in providers.PROVIDERS.items():
        assert protocols, provider
        for protocol, config in protocols.items():
            unmapped = set(config.required_env) - set(config.env_map.values())
            assert not unmapped, f"{provider}/{protocol}: {unmapped}"


def test_choose_protocol_explicit_request_wins(monkeypatch):
    monkeypatch.setenv("SURFSHARK_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("SURFSHARK_OPENVPN_USER", "u")
    monkeypatch.setenv("SURFSHARK_OPENVPN_PASSWORD", "p")
    assert choose_protocol("surfshark", requested="openvpn") == "openvpn"


def test_choose_protocol_prefers_active_current_then_default(monkeypatch):
    """The running protocol wins only if still credentialed; else the default."""
    monkeypatch.setenv("SURFSHARK_WIREGUARD_PRIVATE_KEY", "k")
    assert active_protocols("surfshark") == ["wireguard"]
    assert choose_protocol("surfshark", current="wireguard") == "wireguard"
    assert choose_protocol("surfshark", current="openvpn") == DEFAULT_PROTOCOL
    assert choose_protocol("surfshark") == DEFAULT_PROTOCOL


def test_resolve_provider_normalizes_and_defaults(monkeypatch):
    monkeypatch.setenv("SURFSHARK_WIREGUARD_PRIVATE_KEY", "k")
    assert resolve_provider("SurfShark") == ("surfshark", DEFAULT_PROTOCOL)
    assert resolve_provider("surfshark", "wireguard") == ("surfshark", "wireguard")


def test_resolve_provider_unknown_is_friendly_exit():
    with pytest.raises(SystemExit, match="Unknown provider 'nordvpn'"):
        resolve_provider("nordvpn")


def test_protocol_config_env_map_is_read_only():
    """frozen=True is only honest if the mapping is read-only too.

    PROVIDERS is module state consulted by every command, so a caller that could
    rewrite a credential mapping would change provider behaviour globally.
    """
    config = PROVIDERS["surfshark"]["wireguard"]
    with pytest.raises(TypeError):
        config.env_map["WIREGUARD_PRIVATE_KEY"] = "HACKED"  # type: ignore[index]
    assert config.env_map["WIREGUARD_PRIVATE_KEY"] == "SURFSHARK_WIREGUARD_PRIVATE_KEY"
    assert PROVIDERS["surfshark"]["wireguard"].env_map["WIREGUARD_PRIVATE_KEY"] == (
        "SURFSHARK_WIREGUARD_PRIVATE_KEY"
    )
