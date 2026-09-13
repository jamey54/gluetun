"""CLI commands for the vpn package.

Runtime selection changes hot-swap through gluetun's control server
(vpn.apply); compose is only used for container lifecycle (create, recreate,
stop) and logs.
"""

import contextlib
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any, TypeVar

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
    DEFAULT_PROTOCOL,
    DEFAULT_SCAN_SIZE_MB,
    DEFAULT_SIZE_MB,
    DEFAULT_TEST_CONCURRENCY,
    DOWN_TIMEOUT_S,
)
from vpn.countries import resolve_country
from vpn.discovery import _state, instance_records, print_ls_table, selection_doc
from vpn.docker import (
    GLUETUN_IMAGE,
    compose,
    container_env,
    container_image,
    container_running,
    container_status,
    run,
)
from vpn.instance import (
    DEFAULT_INSTANCE,
    Instance,
    allocate_free_port,
    current_instance,
    ensure_compose_file,
    env_lookup,
    instance_context,
    read_registry,
    resolve_default_name,
    resolve_instance,
    write_registry,
)
from vpn.ipinfo import _probe, current_exit_ip, print_ip_status, real_ip
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
        port = current_instance().control_port
        raise SystemExit(
            "HTTP_CONTROL_SERVER_API_KEY is not set.\n"
            f"It authenticates gluetun's control server (port {port}) "
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


F = TypeVar("F", bound=Callable[..., Any])


def add_instance_options(
    ctl_port: bool = False, env_file: bool = False
) -> Callable[[F], F]:
    """Option decorator for the shared per-instance switches.

    Apply it as the outermost option decorator (directly under the @command
    decorator) so --instance is listed first in help output: click lists
    options in reverse application order.
    """

    def decorate(func: F) -> F:
        if env_file:
            func = click.option(
                "--env-file",
                type=click.Path(exists=True, dir_okay=False, path_type=str),
                default=None,
                help="Env file replacing .env for this instance (compose --env-file)",
            )(func)
        if ctl_port:
            func = click.option(
                "--ctl-port",
                type=click.IntRange(1, 65535),
                default=None,
                help="Control server host port (defaults to the instance's registered port)",
            )(func)
        func = click.option(
            "--instance",
            default=None,
            help=f"Instance name (default: '{DEFAULT_INSTANCE}')",
        )(func)
        return func

    return decorate


def _resolve_for_command(
    instance: str | None,
    ctl_port: int | None = None,
    env_file: str | None = None,
) -> Instance:
    """Resolve the target instance: --instance > app env > default; honor GLUETUN_CTL_PORT."""
    name = instance or resolve_default_name()
    base = resolve_instance(name, env_file=env_file)
    port = ctl_port
    if port is None:
        env_port = base.env.get("GLUETUN_CTL_PORT")
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                raise click.UsageError(
                    f"GLUETUN_CTL_PORT must be a port number, got {env_port!r}"
                ) from None
    if port is not None and port != base.control_port:
        base = replace(base, control_port=port)
    return base


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


def _baked_selection() -> Selection | None:
    """Selection baked into the container at create time (its compose env)."""
    env = container_env()
    provider = env.get("VPN_SERVICE_PROVIDER")
    if not provider:
        return None
    return Selection(
        provider, env.get("VPN_TYPE") or "", env.get("VPN_COUNTRY"), env.get("VPN_CITY")
    )


def _status_doc() -> dict[str, object]:
    """Stable status document (status --json). Called inside the instance context."""
    inst = current_instance()
    state = _state(inst.name)
    sel: Selection | None = None
    enabled = False
    last_error: str | None = None
    if state in ("running", "starting"):
        try:
            sel = Selection.from_doc(control.get_settings())
        except control.ControlError as exc:
            status = f"HTTP {exc.status}" if exc.status is not None else "connection failed"
            last_error = f"control server unreachable ({status}): {exc.message}"
        enabled = bool(sel and sel.provider)

    baked = _baked_selection() if state in ("running", "starting") else None
    drift = bool(sel is not None and sel.provider and baked is not None and sel.key != baked.key)

    exit_ip: dict[str, str] | None = None
    leak = False
    if state == "running":
        result = _probe()
        if result is None:
            leak = True
        else:
            info = result.info
            ip = str(info.get("ip") or "")
            exit_ip = {
                "ip": ip,
                "country": resolve_country(str(info.get("country") or "")),
            }
            bare = real_ip()
            if bare and ip == bare:
                leak = True
    return {
        "instance": inst.name,
        "container_name": inst.name,
        "image": container_image() if state != "absent" else None,
        "state": state,
        "selection": selection_doc(sel) if sel and sel.provider else None,
        "drift": drift,
        "control_server": {"port": inst.control_port, "enabled": enabled},
        "exit_ip": exit_ip,
        "leak": leak,
        "last_error": last_error,
    }


def _print_target(sel: Selection) -> str:
    loc = ", ".join(filter(None, [sel.city, sel.country]))
    return f"{sel.provider}/{sel.protocol}" + (f" → {loc}" if loc else "")


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
@add_instance_options(ctl_port=True, env_file=True)
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
    instance: str | None,
    ctl_port: int | None,
    env_file: str | None,
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
    inst = _resolve_for_command(instance, ctl_port, env_file)
    # A fresh non-default instance gets a free host port allocated and
    # persisted, so its registry survives restarts without ever colliding with
    # the default instance's 8000.
    if (
        ctl_port is None
        and inst.env.get("GLUETUN_CTL_PORT") is None
        and inst.name != DEFAULT_INSTANCE
        and read_registry(inst.name) is None
        and not container_running(name=inst.name)
    ):
        inst = replace(inst, control_port=allocate_free_port())
    ensure_compose_file(inst)
    with instance_context(inst):
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
            raise SystemExit(
                "Cannot read runtime settings — is gluetun's control server reachable?"
            )

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
            compose(
                "up",
                "-d",
                *(("--force-recreate",) if recreate else ()),
                env_overrides=overrides,
            )
            write_registry(inst)
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

        verified = finish_connection(
            expected_country=target.country if (swapped or (requested and not recreate)) else None,
            speedtest=not no_speedtest,
            exclude_ips={prev_ip} if (prev_ip and swapped) else None,
        )
    if not verified:
        raise SystemExit(1)


@main.command()
@add_instance_options()
@click.option("--provider", help="VPN provider")
@click.option("--protocol", type=PROTOCOL, default=None, help="VPN protocol")
@click.option("--country", help="Country to connect to")
@click.option("--city", help="City within the country")
@click.option("--list", "list_servers", is_flag=True, help="List available servers and exit")
@click.option("--no-speedtest", is_flag=True, help="Skip the post-connect speed test")
def connect(
    instance: str | None,
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
    inst = _resolve_for_command(instance)
    with instance_context(inst):
        require_api_key()
        if not container_running():
            raise SystemExit(
                f"Container '{current_instance().container}' is not running. Use 'vpn up' first."
            )
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
        verified = finish_connection(
            expected_country=target.country,
            speedtest=not no_speedtest,
            # Only exclude when we actually moved; when already on target the
            # tunnel still exits via prev_ip itself, which must count as success.
            exclude_ips={prev_ip} if (prev_ip and swapped) else None,
        )
    if not verified:
        raise SystemExit(1)


@main.command()
@add_instance_options()
def down(instance: str | None) -> None:
    """Stop the VPN container."""
    with instance_context(_resolve_for_command(instance)):
        with contextlib.suppress(Exception):
            control.set_vpn_status("stopped", timeout=DOWN_TIMEOUT_S)
        compose("down")
        click.echo("VPN stopped.")


@main.command()
@add_instance_options()
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
@click.option("-n", "--tail", default="50", help="Number of lines to show")
def logs(instance: str | None, follow: bool, tail: str) -> None:
    """Show container logs."""
    with instance_context(_resolve_for_command(instance)):
        args: list[str] = ["logs"]
        if follow:
            args.append("-f")
        args.extend(["--tail", tail, current_instance().container])
        compose(*args)


def _kv(label: str, value: str, color: str | None = None) -> None:
    """Print an indented key-value line with a bold label."""
    text = f"  {label:<12}{value}"
    if color:
        click.echo(click.style(text, fg=color))
    else:
        click.echo(text)


@main.command()
@add_instance_options()
@click.option(
    "-s",
    "--size",
    type=click.IntRange(min=1),
    default=DEFAULT_SIZE_MB,
    show_default=True,
    help="Speed test download size (MB)",
)
@click.option("--no-speedtest", is_flag=True, help="Skip the speed test")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Machine-readable status (single-line JSON)",
)
def status(
    instance: str | None, size: int, no_speedtest: bool, json_output: bool
) -> None:
    """Show container state, effective selection, public IP, and speed test."""
    with instance_context(_resolve_for_command(instance)):
        if json_output:
            doc = _status_doc()
            click.echo(json.dumps(doc))
            if doc["leak"]:
                raise SystemExit(1)
            return
        state = container_status()
        if not state:
            click.echo(f"Container '{current_instance().container}' not found.")
            return

        container = current_instance().container
        _kv("Container", f"{container} ({state})")

        try:
            vpn = control.get_vpn_status()
            _kv("Tunnel", vpn, "red" if vpn == "stopped" else None)
        except control.ControlError:
            pass
        try:
            dns = control.get_dns_status()
            _kv("DNS", dns, "red" if dns == "stopped" else None)
        except control.ControlError:
            pass
        try:
            port = control.get_port_forward()
            if port:
                _kv("Port fwd", str(port))
        except control.ControlError:
            pass

        current = effective_selection()
        if current and current.provider:
            click.echo()
            _kv("Provider", current.provider)
            _kv("Protocol", current.protocol or "?")
            if current.country:
                loc = ", ".join(filter(None, [current.city, current.country]))
                _kv("Location", loc)
        else:
            click.echo()
            _kv("Provider", "unknown — is gluetun's control server reachable?")

        click.echo()
        finish_connection(speedtest=not no_speedtest, size=size)


@main.command()
@add_instance_options()
@click.option("--provider", help="Only bench this provider (default: all credentialed)")
@click.option(
    "--protocol",
    type=PROTOCOL,
    default=None,
    help="Only bench this protocol (default: all credentialed)",
)
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
    "-c",
    "--concurrency",
    type=click.IntRange(min=1),
    default=DEFAULT_TEST_CONCURRENCY,
    show_default=True,
    help="Candidates tested in parallel on temporary containers (1 = test on the running one)",
)
@click.option(
    "--connect",
    is_flag=True,
    help="Connect to the fastest location after benchmarking (default: keep the current location)",
)
def bench(
    instance: str | None,
    provider: str | None,
    protocol: str | None,
    country: str | None,
    max_candidates: int,
    top: int,
    scan_size: int,
    size: int,
    concurrency: int,
    connect: bool,
) -> None:
    """Benchmark locations and report the fastest.

    By default every credentialed provider/protocol is benched; narrow with
    --provider/--protocol/--country, or cap scale with -n/--max-candidates.
    The current location is kept unless --connect is passed.
    """
    with instance_context(_resolve_for_command(instance)):
        require_api_key()
        if not container_running():
            raise SystemExit(f"Container '{current_instance().container}' is not running.")
        by_provider = listable_servers(get_servers())
        if not any(by_provider.values()):
            raise SystemExit("No servers found. Is Docker running?")

        try:
            control.get_settings()
        except control.ControlError as exc:
            raise SystemExit(f"Cannot reach the gluetun control server: {exc}") from None

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
                concurrency=concurrency,
                connect_winner=connect,
            )
        except KeyboardInterrupt:
            raise SystemExit(130) from None

        print_report(report)
        if report.action:
            click.echo(report.action)


@main.command()
@add_instance_options()
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Machine-readable list (single-line JSON)",
)
def ls(instance: str | None, json_output: bool) -> None:
    """List instances (registry and vpn-* compose containers) and their consumers."""
    records = instance_records()
    if instance:
        records = [r for r in records if r["instance"] == instance]
    if json_output:
        click.echo(json.dumps({"default": resolve_default_name(), "instances": records}))
        return
    print_ls_table(records)


@main.command()
@add_instance_options()
@click.argument("action", required=False, type=click.Choice(["on", "off"]))
def dns(instance: str | None, action: str | None) -> None:
    """Show or toggle the DNS-over-TLS resolver."""
    with instance_context(_resolve_for_command(instance)):
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
@add_instance_options()
def update(instance: str | None) -> None:
    """Trigger a server list update."""
    with instance_context(_resolve_for_command(instance)):
        try:
            control.trigger_updater()
        except control.ControlError as exc:
            raise SystemExit(f"Cannot reach control server: {exc}") from None
        click.echo("Server list update triggered.")
