"""Public IP probing, leak detection, and connection verification.

Verification is leak-first: an observation counts as "connected" only when the
observed exit IP differs from the host's bare public IP. Country matching is
advisory — a VPN exit that geolocates elsewhere (virtual locations) is a
warning, not a failure.
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
_real_ip_info: dict[str, object] | None = None


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
    _fetch_real_ip_info()
    return _real_ip_cache


def real_ip_info() -> dict[str, object] | None:
    """Full ipinfo.io response for the host's bare IP. None if unreachable."""
    if _real_ip_info is not None:
        return _real_ip_info
    _fetch_real_ip_info()
    return _real_ip_info


def _fetch_real_ip_info() -> None:
    """Fetch and cache the full host IP response from ipinfo.io."""
    global _real_ip_cache, _real_ip_info
    override = os.getenv("VPN_REAL_IP")
    if override:
        _real_ip_cache = override
        return
    try:
        with urlopen(IP_INFO_URL, timeout=REAL_IP_TIMEOUT_S) as response:
            data = json.loads(response.read().decode(errors="replace"))
        if isinstance(data, dict) and data:
            _real_ip_info = data
            ip = str(data.get("ip") or "")
            _real_ip_cache = ip or None
    except (OSError, json.JSONDecodeError):
        pass


def _same_country(a: str, b: str) -> bool:
    """Compare two countries by code or name, accent- and case-insensitively."""
    return bool(a) and bool(b) and fold(resolve_country(a)) == fold(resolve_country(b))


def _probe() -> dict[str, object] | None:
    """One public-IP probe from inside the container. None on failure."""
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


def _fmt_row(label: str, info: dict[str, object], color: str | None = None) -> str:
    """Format one row of IP info: IP, location, org."""
    ip = str(info.get("ip", "?"))
    country = resolve_country(str(info.get("country", "?")))
    location = f"{info.get('city', '?')}, {country}"
    org = str(info.get("org", "?"))
    text = f"{label:<10} {ip:<20} {location:<25} {org}"
    if color:
        return click.style(text, fg=color)
    return text


def print_ip_status(
    expected_country: str | None = None,
    exclude_ips: Iterable[str] | None = None,
) -> bool:
    """Fetch and display VPN and host IP info side by side with a tri-state verdict.

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
    host = real_ip_info()

    vpn_color: str | None = None
    if expected_country and not outcome.result.matched:
        vpn_color = "yellow"
    elif expected_country:
        vpn_color = "green"

    header = f"{'':10} {'IP':<20} {'Location':<25} {'Org'}"
    click.echo(header)
    click.echo(_fmt_row("VPN", info, vpn_color))
    if host:
        click.echo(_fmt_row("Bare IP", host, "bright_black"))
    return True


def current_exit_ip(retries: int = CURRENT_EXIT_IP_RETRIES) -> str | None:
    """Best-effort single-shot read of the container's current exit IP."""
    outcome = fetch_ip_info(retries=retries)
    info = (outcome.result.info if outcome.result else None) or outcome.last_info
    return str(info.get("ip")) if info else None
