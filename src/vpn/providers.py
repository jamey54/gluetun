"""VPN provider registry and credential handling."""

import os
from dataclasses import dataclass

# Surfshark accepts a bare private key, ProtonVPN requires the WireGuard
# address as well -- hence the require_addresses flag below.


@dataclass(frozen=True)
class ProtocolConfig:
    """How a provider's credentials map onto Gluetun's generic env vars."""

    required_env: tuple[str, ...]
    env_map: dict[str, str]


def _wireguard(provider: str, *, require_addresses: bool = False) -> ProtocolConfig:
    prefix = provider.upper()
    env_map = {
        "WIREGUARD_PRIVATE_KEY": f"{prefix}_WIREGUARD_PRIVATE_KEY",
        "WIREGUARD_ADDRESSES": f"{prefix}_WIREGUARD_ADDRESSES",
    }
    required = [env_map["WIREGUARD_PRIVATE_KEY"]]
    if require_addresses:
        required.append(env_map["WIREGUARD_ADDRESSES"])
    return ProtocolConfig(required_env=tuple(required), env_map=env_map)


def _openvpn(provider: str) -> ProtocolConfig:
    prefix = provider.upper()
    env_map = {
        "OPENVPN_USER": f"{prefix}_OPENVPN_USER",
        "OPENVPN_PASSWORD": f"{prefix}_OPENVPN_PASSWORD",
    }
    return ProtocolConfig(required_env=tuple(env_map.values()), env_map=env_map)


PROVIDERS: dict[str, dict[str, ProtocolConfig]] = {
    provider: {
        "wireguard": _wireguard(provider, require_addresses=provider == "protonvpn"),
        "openvpn": _openvpn(provider),
    }
    for provider in ("surfshark", "protonvpn")
}

DEFAULT_PROTOCOL = "wireguard"


def get_protocols(provider: str) -> list[str]:
    """Protocols a provider supports."""
    return list(PROVIDERS[provider])


def active_protocols(provider: str) -> list[str]:
    """Provider protocols whose required env vars are all set."""
    config = PROVIDERS[provider]
    return [
        protocol
        for protocol, protocol_config in config.items()
        if all(os.getenv(var) for var in protocol_config.required_env)
    ]


def get_active_providers() -> set[tuple[str, str]]:
    """Return {(provider, protocol)} pairs with all required env vars set."""
    return {
        (provider, protocol) for provider in PROVIDERS for protocol in active_protocols(provider)
    }


def choose_protocol(provider: str, requested: str | None = None, current: str | None = None) -> str:
    """Protocol to use: an explicit request wins, then the running one if still
    credentialed, then the default. The result must still pass validate_provider."""
    active = active_protocols(provider)
    if requested is not None:
        return requested.lower()
    if current in active:
        return current
    return next((p for p in (DEFAULT_PROTOCOL, *active) if p in active), DEFAULT_PROTOCOL)


def validate_provider(name: str, protocol: str = DEFAULT_PROTOCOL) -> tuple[str, str]:
    """Validate provider/protocol and check required env vars.

    Returns (lowercase name, protocol).
    """
    name = name.lower()
    if name not in PROVIDERS:
        valid = ", ".join(sorted(PROVIDERS))
        raise SystemExit(f"Unknown provider '{name}'. Available: {valid}")
    if protocol not in PROVIDERS[name]:
        protocols = ", ".join(PROVIDERS[name])
        raise SystemExit(f"Unknown protocol '{protocol}' for {name}. Available: {protocols}")
    missing = [var for var in PROVIDERS[name][protocol].required_env if not os.getenv(var)]
    if missing:
        others = [p for p in active_protocols(name) if p != protocol]
        hint = f" — or pass --protocol {'/'.join(others)}" if others else ""
        raise SystemExit(f"Missing env vars for {name} ({protocol}): {', '.join(missing)}{hint}")
    return name, protocol


def get_provider_env(provider: str, protocol: str) -> dict[str, str]:
    """Map provider-specific env vars to Gluetun's generic env vars."""
    overrides: dict[str, str] = {"VPN_SERVICE_PROVIDER": provider, "VPN_TYPE": protocol}
    for gluetun_var, provider_var in PROVIDERS[provider][protocol].env_map.items():
        value = os.getenv(provider_var)
        if value:
            overrides[gluetun_var] = value
    return overrides
