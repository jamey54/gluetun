"""Public IP probing, leak detection, and connection verification.

Verification is leak-first: an observation counts as "connected" only when the
observed exit IP differs from the host's bare public IP. Country matching is
advisory — a VPN exit that geolocates elsewhere (virtual locations) is a
warning, not a failure.

Two probing strategies:
- control (default): GET /v1/publicip/ip for speed, then ipinfo.io/{ip} for geo.
- docker: docker exec wget ipinfo.io inside the container (one shot, slower).
"""

import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.request import urlopen

import click

from vpn.config import (
    CONTAINER,
    CURRENT_EXIT_IP_RETRIES,
    DEFAULT_PROBER,
    IP_FETCH_DELAY,
    IP_FETCH_RETRIES,
    IP_INFO_URL,
    PROBE_TIMEOUT,
    REAL_IP_TIMEOUT_S,
)
from vpn.countries import resolve_country
from vpn.docker import run
from vpn.textutil import fold

_real_ip_cache: str | None = None
_prober: str = DEFAULT_PROBER


@dataclass
class IpResult:
    """An accepted public-IP observation (not excluded as bare/unchanged)."""

    info: dict[str, object]
    matched: bool  # country matches expected_country; always True when none given


@dataclass
class IpOutcome:
    """Outcome of a verification poll."""

    result: IpResult | None = None  # None = never observed an acceptable IP
    last_info: dict[str, object] | None = None  # last raw observation, accepted or not


def real_ip() -> str | None:
    """The host's bare public IP, cached per process. None if it can't be fetched."""
    global _real_ip_cache
    override = os.getenv("VPN_REAL_IP")
    if override:
        return override
    if _real_ip_cache is not None:
        return _real_ip_cache
    try:
        with urlopen(IP_INFO_URL, timeout=REAL_IP_TIMEOUT_S) as response:
            data = json.loads(response.read().decode(errors="replace"))
        ip = str(data.get("ip") or "")
        _real_ip_cache = ip or None
    except (OSError, json.JSONDecodeError):
        return None
    return _real_ip_cache


def _same_country(a: str, b: str) -> bool:
    """Compare two countries by code or name, accent- and case-insensitively."""
    return bool(a) and bool(b) and fold(resolve_country(a)) == fold(resolve_country(b))


def set_prober(value: str) -> None:
    """Override the probing strategy ('control' or 'docker')."""
    global _prober
    _prober = value


def _fetch_geo_for_ip(ip: str) -> dict[str, object] | None:
    """Query ipinfo.io from the host for geolocation of a known IP."""
    try:
        with urlopen(f"{IP_INFO_URL}/{ip}/json", timeout=PROBE_TIMEOUT) as response:
            data = json.loads(response.read().decode(errors="replace"))
        return data if isinstance(data, dict) and data else None
    except (OSError, json.JSONDecodeError):
        return None


def _probe_control() -> dict[str, object] | None:
    """Fast probe via the control server for IP, then ipinfo.io for geo."""
    from vpn.control import ControlError, get_public_ip

    try:
        ip = get_public_ip()
    except ControlError:
        return None
    if not ip:
        return None
    info = _fetch_geo_for_ip(ip)
    if info is None:
        # IP-only fallback — no country/city available.
        return {"ip": ip}
    return info


def _probe_docker() -> dict[str, object] | None:
    """One public-IP probe via docker exec inside the container."""
    result = run(
        "docker",
        "exec",
        CONTAINER,
        "timeout",
        str(PROBE_TIMEOUT),
        "wget",
        "-T",
        str(PROBE_TIMEOUT),
        "-qO-",
        IP_INFO_URL,
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return info if isinstance(info, dict) and info else None


def _probe() -> dict[str, object] | None:
    """One public-IP probe. Dispatches to the configured strategy."""
    if _prober == "docker":
        return _probe_docker()
    return _probe_control()


def fetch_ip_info(
    retries: int = IP_FETCH_RETRIES,
    delay: float = IP_FETCH_DELAY,
    expected_country: str | None = None,
    exclude_ips: Iterable[str] | None = None,
) -> IpOutcome:
    """Poll until the container's exit IP is outside exclude_ips (bare IP included).

    Stops at the first acceptable observation — country never gates success,
    `expected_country` only sets IpResult.matched. Progress notices distinguish
    leaks (still on the bare connection) from stale routes (previous exit).
    """
    exclude = set(exclude_ips or ())
    bare = real_ip()
    if bare:
        exclude.add(bare)

    last_info: dict[str, object] | None = None
    for attempt in range(retries):
        info = _probe()
        if info is None:
            if 0 < attempt < retries - 1:
                click.echo(f"Waiting for public IP... ({attempt + 1}/{retries})")
        elif (ip := str(info.get("ip") or "")) and ip in exclude:
            last_info = info
            if bare and ip == bare:
                click.echo(f"Leak: traffic not going through VPN ({attempt + 1}/{retries})")
            else:
                country = resolve_country(str(info.get("country", "")))
                click.echo(f"Still routed via {country} ({attempt + 1}/{retries})")
        else:
            matched = not expected_country or _same_country(
                str(info.get("country", "")), expected_country
            )
            return IpOutcome(IpResult(info=info, matched=matched), info)
        if attempt < retries - 1:
            time.sleep(delay)
    return IpOutcome(last_info=last_info)


def print_ip_status(
    expected_country: str | None = None,
    exclude_ips: Iterable[str] | None = None,
) -> bool:
    """Fetch and display IP info with a tri-state verdict.

    Green: VPN exit in the requested country. Yellow: real VPN exit but wrong
    country (virtual location / geo mismatch) — warned, still connected.
    Red: leak (bare connection) or unreachable.

    Returns True only when traffic verifiably exits through the VPN.
    """
    outcome = fetch_ip_info(expected_country=expected_country, exclude_ips=exclude_ips)
    if outcome.result is None:
        last = outcome.last_info or {}
        bare = real_ip()
        if bare and str(last.get("ip") or "") == bare:
            click.echo(
                click.style(
                    "LEAK: traffic exits via your bare connection — the VPN is not up.", fg="red"
                )
            )
        else:
            click.echo("Could not fetch public IP.")
        return False

    info = outcome.result.info
    ip = str(info.get("ip", "?"))
    country = resolve_country(str(info.get("country", "?")))
    location = f"{info.get('city', '?')}, {country}"
    if expected_country and not outcome.result.matched:
        location += f" (requested {expected_country})"
        location = click.style(location, fg="yellow")
    elif expected_country:
        location = click.style(location, fg="green")
    click.echo(f"IP:       {ip}")
    click.echo(f"Location: {location}")
    click.echo(f"Org:      {info.get('org', '?')}")
    return True


def current_exit_ip(retries: int = CURRENT_EXIT_IP_RETRIES) -> str | None:
    """Best-effort single-shot read of the container's current exit IP."""
    outcome = fetch_ip_info(retries=retries)
    info = (outcome.result.info if outcome.result else None) or outcome.last_info
    return str(info.get("ip")) if info else None
