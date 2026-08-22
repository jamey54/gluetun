"""VPN provider registry and credential handling."""

import os

PROVIDERS = {
    "surfshark": {
        "required_env": ["SURFSHARK_WIREGUARD_PRIVATE_KEY"],
        "env_map": {
            "WIREGUARD_PRIVATE_KEY": "SURFSHARK_WIREGUARD_PRIVATE_KEY",
            "WIREGUARD_ADDRESSES": "SURFSHARK_WIREGUARD_ADDRESSES",
        },
    },
    "protonvpn": {
        "required_env": ["PROTONVPN_WIREGUARD_PRIVATE_KEY", "PROTONVPN_WIREGUARD_ADDRESSES"],
        "env_map": {
            "WIREGUARD_PRIVATE_KEY": "PROTONVPN_WIREGUARD_PRIVATE_KEY",
            "WIREGUARD_ADDRESSES": "PROTONVPN_WIREGUARD_ADDRESSES",
        },
    },
}


def validate_provider(name):
    """Validate provider name and check required env vars. Returns lowercase name."""
    name = name.lower()
    if name not in PROVIDERS:
        valid = ", ".join(sorted(PROVIDERS))
        raise SystemExit(f"Unknown provider '{name}'. Available: {valid}")
    missing = [v for v in PROVIDERS[name]["required_env"] if not os.getenv(v)]
    if missing:
        raise SystemExit(f"Missing env vars for {name}: {', '.join(missing)}")
    return name


def get_active_providers():
    """Return providers whose required env vars are all set."""
    return {
        name: cfg
        for name, cfg in PROVIDERS.items()
        if all(os.getenv(v) for v in cfg["required_env"])
    }


def get_provider_env(provider):
    """Map provider-specific env vars to Gluetun's generic env vars."""
    overrides = {"VPN_SERVICE_PROVIDER": provider}
    for gluetun_var, provider_var in PROVIDERS[provider]["env_map"].items():
        value = os.getenv(provider_var)
        if value:
            overrides[gluetun_var] = value
    return overrides
