"""Gluetun control server client and settings document building (stdlib only).

The settings routes (`GET/PUT /v1/vpn/settings`) exist on recent gluetun
images. PUT merges the posted document over the running settings via
`OverrideWith`, where an empty JSON list is a real override — so a full
GET → mutate → PUT round-trip both updates the location and clears stale
filters in one shot.
"""

import copy
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from vpn.config import DEFAULT_BASE_URL, GET_TIMEOUT_S, PUT_TIMEOUT_S
from vpn.docker import env_lookup
from vpn.providers import get_provider_env

SETTINGS_PATH = "/v1/vpn/settings"

# Location filter lists cleared between candidates so no stale value survives.
_LOCATION_FILTERS = ("regions", "categories", "isps", "hostnames", "names", "numbers")


class ControlError(RuntimeError):
    """Control server request failed. status is None for connection errors."""

    def __init__(self, status: int | None, message: str) -> None:
        self.status = status
        self.message = message
        label = f"HTTP {status}" if status is not None else "connection failed"
        super().__init__(f"control server: {label}: {message}")


def base_url() -> str:
    """Control server base URL (env-overridable, loopback by default)."""
    address = env_lookup("HTTP_CONTROL_SERVER_ADDRESS")
    return (address or DEFAULT_BASE_URL).rstrip("/")


def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = GET_TIMEOUT_S,
) -> tuple[int, str]:
    """Perform one authenticated request; return (status, body)."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(f"{base_url()}{path}", data=data, method=method)
    api_key = env_lookup("HTTP_CONTROL_SERVER_API_KEY")
    if api_key:
        request.add_header("X-API-Key", api_key)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode(errors="replace")
    except TimeoutError as exc:
        raise ControlError(None, f"timed out after {timeout}s") from exc
    except HTTPError as exc:
        message = exc.read().decode(errors="replace").strip()
        raise ControlError(exc.code, message or exc.reason.__str__()) from exc
    except URLError as exc:
        raise ControlError(None, str(exc.reason)) from exc


# ---------------------------------------------------------------------------
# VPN settings
# ---------------------------------------------------------------------------


def get_settings() -> dict[str, Any]:
    """Fetch the full VPN settings document."""
    _, body = _request("GET", SETTINGS_PATH)
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ControlError(None, f"invalid JSON from {SETTINGS_PATH}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ControlError(None, f"unexpected payload from {SETTINGS_PATH}")
    return doc


def put_settings(doc: dict[str, Any]) -> str:
    """Apply a settings document (merged server-side); returns the outcome text."""
    _, body = _request("PUT", SETTINGS_PATH, payload=doc, timeout=PUT_TIMEOUT_S)
    return body.strip()


# ---------------------------------------------------------------------------
# VPN status
# ---------------------------------------------------------------------------


def get_vpn_status() -> str:
    """VPN tunnel status: 'running' or 'stopped'."""
    _, body = _request("GET", "/v1/vpn/status")
    return str(json.loads(body).get("status", ""))


def set_vpn_status(status: str, timeout: int = GET_TIMEOUT_S) -> None:
    """Start or stop the VPN tunnel ('running' / 'stopped')."""
    _request("PUT", "/v1/vpn/status", payload={"status": status}, timeout=timeout)


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------


def get_dns_status() -> str:
    """DNS-over-TLS resolver status: 'running' or 'stopped'."""
    _, body = _request("GET", "/v1/dns/status")
    return str(json.loads(body).get("status", ""))


def set_dns_status(status: str) -> None:
    """Start or stop the DNS-over-TLS resolver."""
    _request("PUT", "/v1/dns/status", payload={"status": status})


# ---------------------------------------------------------------------------
# Updater
# ---------------------------------------------------------------------------


def trigger_updater() -> None:
    """Trigger a server list update."""
    _request("PUT", "/v1/updater/status", payload={"status": "running"})


# ---------------------------------------------------------------------------
# Port forwarding
# ---------------------------------------------------------------------------


def get_port_forward() -> int | None:
    """Currently forwarded port, or None if not forwarding."""
    _, body = _request("GET", "/v1/portforward")
    port = json.loads(body).get("port")
    return int(port) if port else None


def with_location(
    doc: dict[str, Any],
    provider: str,
    protocol: str,
    country: str | None = None,
    city: str | None = None,
) -> dict[str, Any]:
    """Return a copy of the settings doc targeting provider/protocol/country/city.

    Also injects the provider's credentials (from the environment/.env) so a
    hot-swap across providers or protocols authenticates, and clears every
    location filter list so no stale value constrains the new selection.
    """
    result = copy.deepcopy(doc)
    result["type"] = protocol

    provider_doc = result.setdefault("provider", {})
    provider_doc["name"] = provider
    selection = provider_doc.setdefault("server_selection", {})
    selection["vpn"] = protocol
    selection["countries"] = [country] if country else []
    selection["cities"] = [city] if city else []
    for filter_name in _LOCATION_FILTERS:
        selection[filter_name] = []

    env = get_provider_env(provider, protocol)
    wireguard = result.setdefault("wireguard", {})
    if private_key := env.get("WIREGUARD_PRIVATE_KEY"):
        wireguard["private_key"] = private_key
    if addresses := env.get("WIREGUARD_ADDRESSES"):
        wireguard["addresses"] = [a.strip() for a in addresses.split(",") if a.strip()]
    openvpn = result.setdefault("openvpn", {})
    if user := env.get("OPENVPN_USER"):
        openvpn["user"] = user
    if password := env.get("OPENVPN_PASSWORD"):
        openvpn["password"] = password
    return result
