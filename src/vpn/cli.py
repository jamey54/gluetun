"""CLI commands for the vpn package."""

import click

from vpn.config import CONTAINER
from vpn.docker import (
    GLUETUN_IMAGE,
    compose,
    container_running,
    container_status,
    env_lookup,
    get_current_vpn,
    run,
)
from vpn.ipinfo import print_ip_status
from vpn.picker import select_server
from vpn.providers import (
    DEFAULT_PROTOCOL,
    PROVIDERS,
    choose_protocol,
    get_provider_env,
    validate_provider,
)
from vpn.servers import (
    get_servers,
    listable_servers,
    parse_server_selection,
    print_servers_table,
)
from vpn.speedtest import DEFAULT_SIZE_MB, format_result, measure

DEBUG = False

SENSITIVE_KEY_PARTS = ("KEY", "PASSWORD", "TOKEN", "SECRET")


def require_api_key() -> None:
    """Fail closed: the control server must never run with an empty API key (M2)."""
    if not env_lookup("HTTP_CONTROL_SERVER_API_KEY"):
        raise SystemExit(
            "HTTP_CONTROL_SERVER_API_KEY is not set.\n"
            "It authenticates gluetun's control server (port 8000) — add any random string to .env."
        )


def _log_env(overrides: dict[str, str]) -> None:
    """Echo compose env overrides when debugging, masking sensitive values."""
    if not DEBUG:
        return
    shown = [
        f"{k}=***" if any(part in k for part in SENSITIVE_KEY_PARTS) else f"{k}={v}"
        for k, v in overrides.items()
    ]
    click.echo(f"Env: {' '.join(shown)}")


def finish_connection(expected_country: str | None = None, speedtest: bool = True) -> bool:
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
def main(debug: bool) -> None:
    """Gluetun VPN manager."""
    global DEBUG
    DEBUG = debug


@main.command()
@click.option("--provider", required=True, help="VPN provider (e.g. surfshark, protonvpn)")
@click.option(
    "--protocol",
    type=click.Choice(sorted({p for cfg in PROVIDERS.values() for p in cfg}), case_sensitive=False),
    default=None,
    help="VPN protocol (default: keep the running one, else wireguard)",
)
@click.option("--no-speedtest", is_flag=True, help="Skip the post-connect speed test")
def up(provider: str, protocol: str | None, no_speedtest: bool) -> None:
    """Start the VPN container, keeping the running location and protocol."""
    require_api_key()
    current = get_current_vpn()
    protocol = choose_protocol(provider, protocol, current.protocol if current else None)
    provider, protocol = validate_provider(provider, protocol)
    overrides = get_provider_env(provider, protocol)
    if current:
        overrides.update(current.location_overrides())
    _log_env(overrides)
    compose("up", "-d", env_overrides=overrides)
    location = overrides.get("SERVER_COUNTRIES", "")
    suffix = f" → {location}" if location else ""
    click.echo(f"VPN started ({provider}/{protocol}){suffix}.")
    finish_connection(expected_country=location or None, speedtest=not no_speedtest)


@main.command()
def down() -> None:
    """Stop the VPN container."""
    compose("down")
    click.echo("VPN stopped.")


@main.command()
@click.option("--no-speedtest", is_flag=True, help="Skip the speed test")
def restart(no_speedtest: bool) -> None:
    """Restart the VPN container."""
    compose("restart")
    click.echo("VPN restarted.")
    finish_connection(speedtest=not no_speedtest)


@main.command()
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
@click.option("-n", "--tail", default="50", help="Number of lines to show")
def logs(follow: bool, tail: str) -> None:
    """Show container logs."""
    args: list[str] = ["logs"]
    if follow:
        args.append("-f")
    args.extend(["--tail", tail, CONTAINER])
    compose(*args)


@main.command()
@click.option("--no-speedtest", is_flag=True, help="Skip the post-connect speed test")
def update(no_speedtest: bool) -> None:
    """Pull latest gluetun image and recreate with the same configuration."""
    require_api_key()
    current = get_current_vpn()
    if not current:
        raise SystemExit("No running container. Use 'vpn up --provider <name>' first.")
    protocol = choose_protocol(current.provider, requested=None, current=current.protocol)
    _, protocol = validate_provider(current.provider, protocol)
    run("docker", "pull", GLUETUN_IMAGE)
    overrides = get_provider_env(current.provider, protocol)
    overrides.update(current.location_overrides())
    _log_env(overrides)
    compose("up", "-d", "--force-recreate", env_overrides=overrides)
    location = current.countries or ""
    suffix = f" → {location}" if location else ""
    click.echo(f"Updated and restarted ({current.provider}/{protocol}){suffix}.")
    finish_connection(expected_country=location or None, speedtest=not no_speedtest)


@main.command()
def ip() -> None:
    """Show the current public VPN IP."""
    print_ip_status()


@main.command()
@click.option(
    "-s",
    "--size",
    type=click.IntRange(min=1),
    default=DEFAULT_SIZE_MB,
    show_default=True,
    help="Download size (MB)",
)
def speedtest(size: int) -> None:
    """Measure download speed through the VPN."""
    if not container_running():
        raise SystemExit(f"Container '{CONTAINER}' is not running.")
    click.echo(f"Downloading {size} MB...")
    result = measure(size)
    if not result:
        raise SystemExit("Speed test failed.")
    click.echo(format_result(result))


@main.command()
@click.option("--no-speedtest", is_flag=True, help="Skip the speed test")
def status(no_speedtest: bool) -> None:
    """Show container status, public IP, and speed test."""
    state = container_status()
    if not state:
        click.echo(f"Container '{CONTAINER}' not found.")
        return
    click.echo(f"Container: {CONTAINER} ({state})")
    finish_connection(speedtest=not no_speedtest)


@main.command()
def servers() -> None:
    """List available servers for all active providers."""
    by_provider = listable_servers(get_servers())
    if not any(by_provider.values()):
        raise SystemExit("No servers found. Is Docker running?")
    print_servers_table(by_provider)


@main.command()
@click.option("--no-speedtest", is_flag=True, help="Skip the post-connect speed test")
def server(no_speedtest: bool) -> None:
    """Interactively select a server and restart."""
    require_api_key()
    by_provider = listable_servers(get_servers())
    if not any(by_provider.values()):
        raise SystemExit("No servers found. Is Docker running?")

    selection = select_server(by_provider)
    if not selection:
        raise SystemExit("No selection.")

    picked_provider, picked_protocol, country, city = parse_server_selection(selection)
    provider, protocol = validate_provider(
        picked_provider or "", picked_protocol or DEFAULT_PROTOCOL
    )

    click.echo(f"Provider: {provider} ({protocol})")
    click.echo(f"Location: {country}" + (f" / {city}" if city else ""))

    compose("down")

    overrides = get_provider_env(provider, protocol)
    overrides["SERVER_COUNTRIES"] = country
    if city:
        overrides["SERVER_CITIES"] = city
    _log_env(overrides)
    compose("up", "-d", env_overrides=overrides)

    location = f"{country}" + (f" / {city}" if city else "")
    click.echo(f"VPN restarted ({provider}/{protocol}) → {location}")
    finish_connection(expected_country=country, speedtest=not no_speedtest)
