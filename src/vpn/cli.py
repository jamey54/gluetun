"""CLI commands for the vpn package."""

import json
import time

import click

from vpn.config import CONTAINER
from vpn.countries import resolve_country
from vpn.docker import GLUETUN_IMAGE, compose, get_current_vpn, run
from vpn.picker import select_server
from vpn.providers import (
    DEFAULT_PROTOCOL,
    PROVIDERS,
    get_provider_env,
    validate_provider,
)
from vpn.servers import (
    get_servers,
    listable_servers,
    parse_server_selection,
    print_servers_table,
    strip_accents,
)
from vpn.speedtest import DEFAULT_SIZE_MB, format_result, measure

IP_INFO_URL = "https://ipinfo.io"
IP_FETCH_RETRIES = 15
IP_FETCH_DELAY = 2
PROBE_TIMEOUT = 8

DEBUG = False


# ---------------------------------------------------------------------------
# IP info
# ---------------------------------------------------------------------------


def _same_country(a, b):
    return strip_accents(resolve_country(a)).lower() == strip_accents(resolve_country(b)).lower()


def container_running():
    result = run(
        "docker", "inspect", "--format", "{{.State.Status}}", CONTAINER,
        capture=True, check=False,
    )
    return result.returncode == 0


def fetch_ip_info(retries=IP_FETCH_RETRIES, delay=IP_FETCH_DELAY, expected_country=None):
    """Fetch public IP info with retries. If expected_country is set, retries until it matches."""
    prev_city = None
    for attempt in range(retries):
        result = run(
            "docker", "exec", CONTAINER,
            "timeout", str(PROBE_TIMEOUT),
            "wget", "-T", str(PROBE_TIMEOUT), "-qO-", IP_INFO_URL,
            capture=True, check=False,
        )
        if result.returncode == 0:
            try:
                info = json.loads(result.stdout)
                if not expected_country:
                    return info
                actual_country = info.get("country", "")
                city = info.get("city", "")
                if actual_country and _same_country(actual_country, expected_country):
                    return info
                if prev_city is not None and city != prev_city:
                    click.echo(f"Location changed to {city}, VPN is connected.")
                    return info
                prev_city = city
                click.echo(
                    f"Public IP: {city or '?'}, {resolve_country(actual_country)}"
                    f" — waiting for {expected_country}... ({attempt + 1}/{retries})"
                )
            except json.JSONDecodeError:
                pass
        elif attempt < retries - 1:
            click.echo(f"Waiting for VPN connection... ({attempt + 1}/{retries})")
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def print_ip_status(expected_country=None):
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
        actual = info.get("country", "")
        verified = bool(actual) and _same_country(actual, expected_country)
        location = click.style(location, fg="green" if verified else "red")
    click.echo(f"Location: {location}")
    click.echo(f"Org:      {info.get('org', '?')}")
    return verified


def finish_connection(expected_country=None, speedtest=True):
    """Show connection status and optionally run a speed test."""
    verified = print_ip_status(expected_country=expected_country)
    if verified and speedtest:
        click.echo("Running speed test...")
        result = measure()
        if result:
            click.echo(format_result(result))
        else:
            click.echo("Speed test failed.")
    elif speedtest:
        click.echo("Skipping speed test — connection not verified.")
    return verified


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@click.group()
@click.option("--debug", is_flag=True, envvar="VPN_DEBUG", help="Enable debug output")
def cli(debug):
    """Gluetun VPN manager."""
    global DEBUG
    DEBUG = debug


@cli.command()
@click.option("--provider", required=True, help="VPN provider (e.g. surfshark, protonvpn)")
@click.option(
    "--protocol",
    type=click.Choice(sorted({p for cfg in PROVIDERS.values() for p in cfg}), case_sensitive=False),
    default=DEFAULT_PROTOCOL,
    help="VPN protocol",
)
@click.option("--no-speedtest", is_flag=True, help="Skip the post-connect speed test")
def up(provider, protocol, no_speedtest):
    """Start the VPN container."""
    provider, protocol = validate_provider(provider, protocol)
    overrides = get_provider_env(provider, protocol)
    if DEBUG:
        click.echo(f"Env: {' '.join(f'{k}={v}' for k, v in overrides.items())}")
    compose("up", "-d", env_overrides=overrides)
    click.echo(f"VPN started ({provider}/{protocol}).")
    finish_connection(speedtest=not no_speedtest)


@cli.command()
def down():
    """Stop the VPN container."""
    compose("down")
    click.echo("VPN stopped.")


@cli.command()
def restart():
    """Restart the VPN container."""
    compose("restart")
    click.echo("VPN restarted.")
    print_ip_status()


@cli.command()
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
@click.option("-n", "--tail", default="50", help="Number of lines to show")
def logs(follow, tail):
    """Show container logs."""
    args = ["logs"]
    if follow:
        args.append("-f")
    args.extend(["--tail", tail, CONTAINER])
    compose(*args)


@cli.command()
@click.option("--no-speedtest", is_flag=True, help="Skip the post-connect speed test")
def update(no_speedtest):
    """Pull latest gluetun image and recreate the container."""
    current = get_current_vpn()
    if not current:
        raise SystemExit("No running container. Use 'vpn up --provider <name>' first.")
    provider, protocol = current
    run("docker", "pull", GLUETUN_IMAGE)
    overrides = get_provider_env(provider, protocol)
    if DEBUG:
        click.echo(f"Env: {' '.join(f'{k}={v}' for k, v in overrides.items())}")
    compose("up", "-d", "--force-recreate", env_overrides=overrides)
    click.echo(f"Updated and restarted ({provider}/{protocol}).")
    finish_connection(speedtest=not no_speedtest)


@cli.command()
def ip():
    """Show the current public VPN IP."""
    print_ip_status()


@cli.command()
@click.option("-s", "--size", type=int, default=DEFAULT_SIZE_MB, show_default=True, help="Download size (MB)")
def speedtest(size):
    """Measure download speed through the VPN."""
    if not container_running():
        raise SystemExit(f"Container '{CONTAINER}' is not running.")
    click.echo(f"Downloading {size} MB...")
    result = measure(size)
    if not result:
        raise SystemExit("Speed test failed.")
    click.echo(format_result(result))


@cli.command()
def status():
    """Show container status and public IP."""
    result = run(
        "docker", "inspect", "--format", "{{.State.Status}}", CONTAINER,
        capture=True, check=False,
    )
    if result.returncode != 0:
        click.echo(f"Container '{CONTAINER}' not found.")
        return
    click.echo(f"Container: {CONTAINER} ({result.stdout.strip()})")
    print_ip_status()


@cli.command()
def servers():
    """List available servers for all active providers."""
    by_provider = listable_servers(get_servers())
    if not any(by_provider.values()):
        raise SystemExit("No servers found. Is Docker running?")
    print_servers_table(by_provider)


@cli.command()
@click.option("--no-speedtest", is_flag=True, help="Skip the post-connect speed test")
def server(no_speedtest):
    """Interactively select a server and restart."""
    by_provider = listable_servers(get_servers())
    if not any(by_provider.values()):
        raise SystemExit("No servers found. Is Docker running?")

    selection = select_server(by_provider)
    if not selection:
        raise SystemExit("No selection.")

    provider, protocol, country, city = parse_server_selection(selection)
    provider, protocol = validate_provider(provider, protocol)

    click.echo(f"Provider: {provider} ({protocol})")
    click.echo(f"Location: {country}" + (f" / {city}" if city else ""))

    compose("down")

    overrides = get_provider_env(provider, protocol)
    overrides["SERVER_COUNTRIES"] = country
    if city:
        overrides["SERVER_CITIES"] = city
    if DEBUG:
        click.echo(f"Env: {' '.join(f'{k}={v}' for k, v in overrides.items())}")
    compose("up", "-d", env_overrides=overrides)

    click.echo(f"VPN restarted ({provider}/{protocol}) → {country}" + (f" / {city}" if city else ""))
    finish_connection(expected_country=country, speedtest=not no_speedtest)
