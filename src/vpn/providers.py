"""VPN provider registry and credential handling."""

import os

PROVIDERS = {
    "surfshark": {
        "wireguard": {
            "required_env": ["SURFSHARK_WIREGUARD_PRIVATE_KEY"],
            "env_map": {
                "WIREGUARD_PRIVATE_KEY": "SURFSHARK_WIREGUARD_PRIVATE_KEY",
                "WIREGUARD_ADDRESSES": "SURFSHARK_WIREGUARD_ADDRESSES",
            },
        },
        "openvpn": {
            "required_env": ["SURFSHARK_OPENVPN_USER", "SURFSHARK_OPENVPN_PASSWORD"],
            "env_map": {
                "OPENVPN_USER": "SURFSHARK_OPENVPN_USER",
                "OPENVPN_PASSWORD": "SURFSHARK_OPENVPN_PASSWORD",
            },
        },
    },
    "protonvpn": {
        "wireguard": {
            "required_env": ["PROTONVPN_WIREGUARD_PRIVATE_KEY", "PROTONVPN_WIREGUARD_ADDRESSES"],
            "env_map": {
                "WIREGUARD_PRIVATE_KEY": "PROTONVPN_WIREGUARD_PRIVATE_KEY",
                "WIREGUARD_ADDRESSES": "PROTONVPN_WIREGUARD_ADDRESSES",
            },
        },
        "openvpn": {
            "required_env": ["PROTONVPN_OPENVPN_USER", "PROTONVPN_OPENVPN_PASSWORD"],
            "env_map": {
                "OPENVPN_USER": "PROTONVPN_OPENVPN_USER",
                "OPENVPN_PASSWORD": "PROTONVPN_OPENVPN_PASSWORD",
            },
        },
    },
}

DEFAULT_PROTOCOL = "wireguard"


def get_protocols(provider):
    """Protocols a provider supports."""
    return list(PROVIDERS[provider])


def active_protocols(provider):
    """Provider protocols whose required env vars are all set."""
    cfg = PROVIDERS[provider]
    return [
        protocol
        for protocol, pcfg in cfg.items()
        if all(os.getenv(v) for v in pcfg["required_env"])
    ]


def get_active_providers():
    """Return {(provider, protocol)} pairs with all required env vars set."""
    return {
        (provider, protocol) for provider in PROVIDERS for protocol in active_protocols(provider)
    }


def choose_protocol(provider, requested=None, current=None):
    """Protocol to use: an explicit request wins, then the running one if still
    credentialed, then the default. The result must still pass validate_provider."""
    active = active_protocols(provider)
    if requested is not None:
        return requested.lower()
    if current in active:
        return current
    return next((p for p in (DEFAULT_PROTOCOL, *active) if p in active), DEFAULT_PROTOCOL)


def validate_provider(name, protocol=DEFAULT_PROTOCOL):
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
    missing = [v for v in PROVIDERS[name][protocol]["required_env"] if not os.getenv(v)]
    if missing:
        others = [p for p in active_protocols(name) if p != protocol]
        hint = f" — or pass --protocol {'/'.join(others)}" if others else ""
        raise SystemExit(f"Missing env vars for {name} ({protocol}): {', '.join(missing)}{hint}")
    return name, protocol


def get_provider_env(provider, protocol):
    """Map provider-specific env vars to Gluetun's generic env vars."""
    overrides = {"VPN_SERVICE_PROVIDER": provider, "VPN_TYPE": protocol}
    for gluetun_var, provider_var in PROVIDERS[provider][protocol]["env_map"].items():
        value = os.getenv(provider_var)
        if value:
            overrides[gluetun_var] = value
    return overrides
