"""Public IP probing and connection verification."""

import json
import time

import click

from vpn.config import CONTAINER
from vpn.countries import resolve_country
from vpn.docker import run
from vpn.textutil import fold

IP_INFO_URL = "https://ipinfo.io"
IP_FETCH_RETRIES = 15
IP_FETCH_DELAY = 2
PROBE_TIMEOUT = 8


def _same_country(a: str, b: str) -> bool:
    """Compare two countries by code or name, accent- and case-insensitively."""
    return bool(a) and bool(b) and fold(resolve_country(a)) == fold(resolve_country(b))


def fetch_ip_info(
    retries: int = IP_FETCH_RETRIES,
    delay: float = IP_FETCH_DELAY,
    expected_country: str | None = None,
) -> dict[str, object] | None:
    """Fetch public IP info with retries. If expected_country is set, retry until it matches."""
    for attempt in range(retries):
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
        if result.returncode == 0:
            try:
                info: dict[str, object] | None = json.loads(result.stdout)
            except json.JSONDecodeError:
                info = None
            if info:
                actual_country = str(info.get("country", ""))
                if not expected_country or _same_country(actual_country, expected_country):
                    return info
                click.echo(
                    f"Public IP: {info.get('city') or '?'}, {resolve_country(actual_country)}"
                    f" — waiting for {expected_country}... ({attempt + 1}/{retries})"
                )
        elif 0 < attempt < retries - 1:
            click.echo(f"Waiting for public IP... ({attempt + 1}/{retries})")
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def print_ip_status(expected_country: str | None = None) -> bool:
    """Fetch and display IP info. Location is green when the country matches.

    Returns True only for a verified connection (or successful fetch when
    no expected country was given).
    """
    info = fetch_ip_info(expected_country=expected_country)
    if not info:
        click.echo("Could not fetch public IP.")
        return False
    click.echo(f"IP:       {info.get('ip', '?')}")
    country = resolve_country(str(info.get("country", "?")))
    location = f"{info.get('city', '?')}, {country}"
    verified = True
    if expected_country:
        verified = _same_country(str(info.get("country", "")), expected_country)
        location = click.style(location, fg="green" if verified else "red")
    click.echo(f"Location: {location}")
    click.echo(f"Org:      {info.get('org', '?')}")
    return verified
