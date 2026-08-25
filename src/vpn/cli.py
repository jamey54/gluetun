"""CLI commands for the vpn package.

Runtime selection changes hot-swap through gluetun's control server
(vpn.apply); compose is only used for container lifecycle (create, recreate,
stop) and logs.
"""

import contextlib

import click

from vpn import control
from vpn.apply import Selection, apply_location
from vpn.bench import (
    DEFAULT_FINAL_SIZE_MB,
    DEFAULT_TOP,
    build_candidates,
    print_report,
    run_bench,
)
from vpn.config import (
    CONTAINER,
    CONTROL_SERVER_PORT,
    DEFAULT_PROTOCOL,
    DEFAULT_SCAN_SIZE_MB,
    DEFAULT_SIZE_MB,
)
from vpn.docker import (
    GLUETUN_IMAGE,
    compose,
    container_env,
    container_running,
    container_status,
    env_lookup,
    run,
)
from vpn.ipinfo import current_exit_ip, print_ip_status
from vpn.picker import select_server
from vpn.providers import (
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
from vpn.speedtest import format_result, measure
from vpn.textutil import fold

DEBUG = False

SENSITIVE_KEY_PARTS = ("KEY", "PASSWORD", "TOKEN", "SECRET")

PROTOCOL = click.Choice(
    sorted({p for cfg in PROVIDERS.values() for p in cfg}), case_sensitive=False
)


def require_api_key() -> None:
    """Fail closed: the control server must never run with an empty API key (M2)."""
    if not env_lookup("HTTP_CONTROL_SERVER_API_KEY"):
        raise SystemExit(
            "HTTP_CONTROL_SERVER_API_KEY is not set.\n"
            f"It authenticates gluetun's control server (port {CONTROL_SERVER_PORT}) "
            "— add any random string to .env."
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


def finish_connection(
    expected_country: str | None = None,
    speedtest: bool = True,
    size: int = DEFAULT_SIZE_MB,
    exclude_ips: set[str] | None = None,
) -> bool:
    """Show connection status and optionally run a speed test."""
    verified = print_ip_status(expected_country=expected_country, exclude_ips=exclude_ips)
    if verified and speedtest:
        click.echo("Running speed test...")
        result = measure(size)
        if result:
            click.echo(format_result(result))
        else:
            click.echo("Speed test failed.")
    elif speedtest:
        click.echo("Skipping speed test — connection not verified.")
    return verified


def effective_selection() -> Selection | None:
    """Runtime selection from the control server; None when unreachable or blank."""
    try:
        return Selection.from_doc(control.get_settings())
    except control.ControlError:
        return None


def _require_selection() -> Selection:
    sel = effective_selection()
    if sel is None or not sel.provider:
        raise SystemExit("Cannot read runtime settings — is gluetun's control server reachable?")
    return sel


def _print_target(sel: Selection) -> str:
    location = (sel.country or "") + (f" / {sel.city}" if sel.city else "")
    return f"{sel.provider}/{sel.protocol}" + (f" → {location}" if location else "")


def _apply_request(
    provider: str | None,
    protocol: str | None,
    country: str | None,
    city: str | None,
    base: Selection,
) -> tuple[Selection, bool]:
    """Resolve the requested target over base and hot-swap; return (target, swapped).

    An explicit country replaces the location outright; a lone city keeps the
    current country (and fails when there is none); switching provider drops
    the old location. Returns swapped=False when the target already matches
    the running state.
    """
    target_provider = provider or base.provider
    target_protocol = protocol or base.protocol or DEFAULT_PROTOCOL
    target_country: str | None
    target_city: str | None
    if country is not None:
        target_country, target_city = country, city
    elif city is not None:
        target_country, target_city = base.country, city
    elif fold(target_provider) != fold(base.provider):
        target_country, target_city = None, None
    else:
        target_country, target_city = base.country, base.city

    if city is not None and not target_country:
        raise SystemExit(
            "--city needs a country to search within: pass --country, or connect "
            "to a country first."
        )

    target_provider, target_protocol = validate_provider(target_provider, target_protocol)
    target = Selection(target_provider, target_protocol, target_country, target_city)
    if target.key == base.key:
        return target, False
    apply_location(target)
    return target, True


def _warn_drift(current: Selection) -> None:
    """Warn when the runtime selection diverges from the compose/.env config."""
    env = container_env()
    configured = Selection(
        env.get("VPN_SERVICE_PROVIDER", ""),
        env.get("VPN_TYPE", ""),
        env.get("SERVER_COUNTRIES") or None,
        env.get("SERVER_CITIES") or None,
    )
    if configured.provider and configured.key != current.key:
        click.echo(
            click.style(
                "Drift: runtime selection differs from the compose/.env config — "
                "recreating the container reverts it.",
                fg="yellow",
            )
        )


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
@click.option("--provider", help="VPN provider (required to start a stopped container)")
@click.option(
    "--protocol",
    type=PROTOCOL,
    default=None,
    help="VPN protocol (default: the running one, else wireguard)",
)
@click.option("--country", help="Country to connect to")
@click.option("--city", help="City within the country")
@click.option("--pull", is_flag=True, help="Pull the latest gluetun image first")
@click.option("--recreate", is_flag=True, help="Recreate the container from compose/.env config")
@click.option("--no-speedtest", is_flag=True, help="Skip the post-connect speed test")
def up(
    provider: str | None,
    protocol: str | None,
    country: str | None,
    city: str | None,
    pull: bool,
    recreate: bool,
    no_speedtest: bool,
) -> None:
    """Start the VPN; apply any requested location via hot-swap.

    With no arguments on a running container this only verifies the tunnel.
    Selections are runtime-only: --pull/--recreate revert to compose/.env config.
    """
    require_api_key()
    requested = any(v is not None for v in (provider, protocol, country, city))
    was_running = container_running()
    current = effective_selection() if was_running else None
    created = not was_running
    if pull:
        run("docker", "pull", GLUETUN_IMAGE)
        recreate = True
    if recreate:
        created = True

    # On a create/recreate path compose already applies provider/protocol;
    # only an explicit location constitutes a further hot-swap request there.
    if created:
        requested = country is not None or city is not None
    if was_running and requested and current is None:
        raise SystemExit("Cannot read runtime settings — is gluetun's control server reachable?")

    swapped = False
    target = Selection("", "")
    prev_ip: str | None = None
    if created:
        name = provider or (current.provider if current else None)
        if not name:
            raise SystemExit("--provider is required to start the container.")
        proto = choose_protocol(name, protocol, current.protocol if current else None)
        name, proto = validate_provider(name, proto)
        overrides = get_provider_env(name, proto)
        _log_env(overrides)
        compose("up", "-d", *(("--force-recreate",) if recreate else ()), env_overrides=overrides)
        click.echo(f"VPN {'recreated' if recreate else 'started'} ({name}/{proto}).")
        # Runtime state now equals env config: the fresh container runs the
        # baked pair with no location, so requests resolve against this.
        current = Selection(name, proto)

    if requested:
        if not created:
            # A hot-swap on a live tunnel must move the exit off this IP.
            prev_ip = current_exit_ip()
        base = current or Selection("", "")
        target, swapped = _apply_request(provider, protocol, country, city, base)
        click.echo(f"{'Swapped to' if swapped else 'Already on'} {_print_target(target)}.")
    elif current is not None and not recreate:
        target = current

    finish_connection(
        expected_country=target.country if (swapped or (requested and not recreate)) else None,
        speedtest=not no_speedtest,
        exclude_ips={prev_ip} if (prev_ip and swapped) else None,
    )


@main.command()
@click.option("--provider", help="VPN provider")
@click.option("--protocol", type=PROTOCOL, default=None, help="VPN protocol")
@click.option("--country", help="Country to connect to")
@click.option("--city", help="City within the country")
@click.option("--list", "list_servers", is_flag=True, help="List available servers and exit")
@click.option("--no-speedtest", is_flag=True, help="Skip the post-connect speed test")
def connect(
    provider: str | None,
    protocol: str | None,
    country: str | None,
    city: str | None,
    list_servers: bool,
    no_speedtest: bool,
) -> None:
    """Hot-swap to another server without restarting; no arguments opens the picker."""
    if list_servers:
        by_provider = listable_servers(get_servers())
        if not any(by_provider.values()):
            raise SystemExit("No servers found. Is Docker running?")
        print_servers_table(by_provider)
        return

    require_api_key()
    if not container_running():
        raise SystemExit(f"Container '{CONTAINER}' is not running. Use 'vpn up' first.")
    current = _require_selection()

    if not any(v is not None for v in (provider, protocol, country, city)):
        by_provider = listable_servers(get_servers())
        if not any(by_provider.values()):
            raise SystemExit("No servers found. Is Docker running?")
        selection = select_server(by_provider)
        if not selection:
            raise SystemExit("No selection.")
        picked_provider, picked_protocol, country, city = parse_server_selection(selection)
        provider, protocol = picked_provider, picked_protocol

    prev_ip = current_exit_ip()
    target, swapped = _apply_request(provider, protocol, country, city, current)
    click.echo(f"{'Swapped to' if swapped else 'Already on'} {_print_target(target)}.")
    finish_connection(
        expected_country=target.country,
        speedtest=not no_speedtest,
        # Only exclude when we actually moved; when already on target the
        # tunnel still exits via prev_ip itself, which must count as success.
        exclude_ips={prev_ip} if (prev_ip and swapped) else None,
    )


@main.command()
def down() -> None:
    """Stop the VPN container."""
    with contextlib.suppress(control.ControlError):
        control.set_vpn_status("stopped")
    compose("down")
    click.echo("VPN stopped.")


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
@click.option(
    "-s",
    "--size",
    type=click.IntRange(min=1),
    default=DEFAULT_SIZE_MB,
    show_default=True,
    help="Speed test download size (MB)",
)
@click.option("--no-speedtest", is_flag=True, help="Skip the speed test")
def status(size: int, no_speedtest: bool) -> None:
    """Show container state, effective selection, public IP, and speed test."""
    state = container_status()
    if not state:
        click.echo(f"Container '{CONTAINER}' not found.")
        return
    click.echo(f"Container: {CONTAINER} ({state})")
    try:
        vpn = control.get_vpn_status()
        click.echo(f"VPN:      {vpn}")
    except control.ControlError:
        pass
    current = effective_selection()
    if current and current.provider:
        click.echo(f"Selection: {_print_target(current)}")
        _warn_drift(current)
    else:
        click.echo("Selection: unknown — is gluetun's control server reachable?")
    try:
        dns = control.get_dns_status()
        click.echo(f"DNS:      {dns}")
    except control.ControlError:
        pass
    try:
        port = control.get_port_forward()
        if port:
            click.echo(f"Port fwd: {port}")
    except control.ControlError:
        pass
    finish_connection(speedtest=not no_speedtest, size=size)


@main.command()
@click.option("--provider", help="Only bench this provider (default: the running one)")
@click.option("--protocol", type=PROTOCOL, default=None, help="Only bench this protocol")
@click.option("--country", help="Only bench this country")
@click.option(
    "-n",
    "--max-candidates",
    type=click.IntRange(min=0),
    default=0,
    help="Cap on locations entering the latency stage (default: no cap)",
)
@click.option(
    "--top",
    type=click.IntRange(min=1),
    default=DEFAULT_TOP,
    show_default=True,
    help="Locations that get a screening download after latency ranking",
)
@click.option(
    "--scan-size",
    type=click.IntRange(min=1),
    default=DEFAULT_SCAN_SIZE_MB,
    show_default=True,
    help="Screening download size (MB)",
)
@click.option(
    "-s",
    "--size",
    type=click.IntRange(min=1),
    default=DEFAULT_FINAL_SIZE_MB,
    show_default=True,
    help="Finals download size (MB)",
)
@click.option(
    "--all",
    "all_providers",
    is_flag=True,
    help="Bench every credentialed provider/protocol, not just the running pair",
)
@click.option(
    "--no-connect",
    is_flag=True,
    help="Do not connect to the winner; restore pre-bench settings instead",
)
def bench(
    provider: str | None,
    protocol: str | None,
    country: str | None,
    max_candidates: int,
    top: int,
    scan_size: int,
    size: int,
    all_providers: bool,
    no_connect: bool,
) -> None:
    """Benchmark locations and connect to the fastest."""
    require_api_key()
    if not container_running():
        raise SystemExit(f"Container '{CONTAINER}' is not running.")
    by_provider = listable_servers(get_servers())
    if not any(by_provider.values()):
        raise SystemExit("No servers found. Is Docker running?")

    try:
        running = Selection.from_doc(control.get_settings())
    except control.ControlError as exc:
        raise SystemExit(f"Cannot reach the gluetun control server: {exc}") from None

    if not all_providers and not provider and not protocol and running.provider:
        provider, protocol = running.provider, running.protocol
    candidates = build_candidates(by_provider, provider, protocol, country)
    if not candidates:
        raise SystemExit("No matching locations for the given filters.")

    try:
        report = run_bench(
            candidates,
            top=top,
            limit=max_candidates,
            scan_size_mb=scan_size,
            final_size_mb=size,
            connect_winner=not no_connect,
        )
    except KeyboardInterrupt:
        raise SystemExit(130) from None

    print_report(report)
    if report.action:
        click.echo(report.action)


@main.command()
@click.argument("action", required=False, type=click.Choice(["on", "off"]))
def dns(action: str | None) -> None:
    """Show or toggle the DNS-over-TLS resolver."""
    if action is None:
        try:
            status = control.get_dns_status()
            click.echo(f"DNS: {status}")
        except control.ControlError as exc:
            raise SystemExit(f"Cannot reach control server: {exc}") from None
        return
    target = "running" if action == "on" else "stopped"
    try:
        control.set_dns_status(target)
    except control.ControlError as exc:
        raise SystemExit(f"Cannot reach control server: {exc}") from None
    click.echo(f"DNS {target}.")


@main.command()
def update() -> None:
    """Trigger a server list update."""
    try:
        control.trigger_updater()
    except control.ControlError as exc:
        raise SystemExit(f"Cannot reach control server: {exc}") from None
    click.echo("Server list update triggered.")
