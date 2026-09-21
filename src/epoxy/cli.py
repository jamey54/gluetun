"""CLI commands for the epoxy package.

Runtime selection changes hot-swap through the container's control server
(epoxy.apply); compose is only used for container lifecycle (create, recreate,
stop) and logs.
"""

import contextlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import click

from epoxy import control, discovery
from epoxy.apply import Selection, apply_location
from epoxy.bench import (
    DEFAULT_FINAL_SIZE_MB,
    DEFAULT_TOP,
    build_candidates,
    print_report,
    run_bench,
)
from epoxy.config import (
    COMPOSE_TIMEOUT_S,
    DEFAULT_PROTOCOL,
    DEFAULT_SCAN_SIZE_MB,
    DEFAULT_SIZE_MB,
    DEFAULT_TEST_CONCURRENCY,
    DOWN_TIMEOUT_S,
    PULL_TIMEOUT_S,
)
from epoxy.countries import resolve_country
from epoxy.discovery import _state, instance_records, print_ls_table
from epoxy.docker import (
    ENGINE_IMAGE,
    compose,
    container_control_port,
    container_env,
    container_image,
    container_running,
    container_status,
    remove_container,
    run,
)
from epoxy.instance import (
    INSTANCE_ENV_VAR,
    Instance,
    allocate_free_port,
    current_instance,
    delete_instance_state,
    ensure_compose_file,
    env_lookup,
    instance_context,
    read_registry,
    resolve_instance,
    write_registry,
)
from epoxy.ipinfo import _probe, current_exit_ip, print_ip_status, real_ip
from epoxy.picker import select_instance, select_server
from epoxy.providers import (
    PROVIDERS,
    get_provider_env,
    resolve_provider,
    validate_provider,
)
from epoxy.servers import (
    get_servers,
    listable_servers,
    parse_server_selection,
    print_servers_table,
)
from epoxy.speedtest import format_result, measure
from epoxy.statusdoc import classify_verdict, control_server_doc, selection_doc
from epoxy.textutil import fold
from epoxy.version import __version__

DEBUG = False

SENSITIVE_KEY_PARTS = ("KEY", "PASSWORD", "TOKEN", "SECRET")

PROTOCOL = click.Choice(
    sorted({p for cfg in PROVIDERS.values() for p in cfg}), case_sensitive=False
)


def require_api_key() -> None:
    """Fail closed: the control server must never run with an empty API key (M2)."""
    if not env_lookup("HTTP_CONTROL_SERVER_API_KEY"):
        port = current_instance().control_port
        raise click.ClickException(
            "HTTP_CONTROL_SERVER_API_KEY is not set.\n"
            f"It authenticates the container's control server (port {port}) "
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


def add_instance_options(ctl_port: bool = False, env_file: bool = False) -> Callable[[F], F]:
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
            help="Instance name (default: $EPOXY_INSTANCE; required when unset)",
        )(func)
        return func

    return decorate


def _no_instance_error() -> NoReturn:
    """The documented no-target error: --instance or EPOXY_INSTANCE required."""
    raise click.UsageError("No instance selected: pass --instance or set EPOXY_INSTANCE.")


def _stdin_is_tty() -> bool:
    """True when the CLI can ask the user interactively."""
    return sys.stdin.isatty()


def _choose_instance_name() -> str:
    """Resolve the target when neither --instance nor EPOXY_INSTANCE is set.

    Interactive terminals pick among the known instances (auto-selecting the
    sole instance without prompting); scripts keep the documented usage error —
    automation must always name its instance explicitly.
    """
    if not _stdin_is_tty():
        _no_instance_error()
    names = sorted(discovery._known_names())
    if not names:
        _no_instance_error()
    if len(names) == 1:
        return names[0]
    chosen = select_instance([(name, _state(name)) for name in names])
    if chosen is None:
        raise click.ClickException("No instance selected.")
    return chosen


def _resolve_for_command(
    instance: str | None,
    ctl_port: int | None = None,
    env_file: str | None = None,
) -> Instance:
    """Resolve the target instance: --instance > EPOXY_INSTANCE > interactive
    choice > usage error, honoring EPOXY_CTL_PORT. A registry-less instance
    falls back to its published control port so imported/shared containers stay
    addressable."""
    name = instance or os.getenv(INSTANCE_ENV_VAR)
    if name is None:
        name = _choose_instance_name()
    base = resolve_instance(name, env_file=env_file)
    port = ctl_port
    if port is None:
        env_port = base.env.get("EPOXY_CTL_PORT")
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                raise click.UsageError(
                    f"EPOXY_CTL_PORT must be a port number, got {env_port!r}"
                ) from None
    if port is None:
        return _apply_published_fallback(base)
    if port != base.control_port:
        base = replace(base, control_port=port)
    return base


def all_option(func: F) -> F:
    """Flag decorator for commands that can act on every known instance.

    Apply it below @add_instance_options so --instance stays listed first in
    help output. Commands receiving it take an ``all_instances`` parameter.
    """
    return click.option(
        "--all",
        "all_instances",
        is_flag=True,
        help="Act on all known instances instead of one",
    )(func)


def _apply_published_fallback(base: Instance) -> Instance:
    """Point a registry-less instance at its published control port, if any."""
    if read_registry(base.name) is None:
        published = container_control_port(name=base.name)
        if published is not None and published != base.control_port:
            return replace(base, control_port=published)
    return base


def _resolve_targets(instance: str | None, all_instances: bool) -> list[Instance]:
    """Resolve one instance, or every known instance for --all (sorted by name).

    --all conflicts with --instance; it ignores EPOXY_INSTANCE and never
    prompts. Registry-less targets fall back to their published control port,
    mirroring single-instance resolution.
    """
    if all_instances:
        if instance is not None:
            raise click.UsageError("--instance and --all are mutually exclusive.")
        names = sorted(discovery._known_names())
        targets = [_apply_published_fallback(resolve_instance(name)) for name in names]
        return targets
    return [_resolve_for_command(instance)]


def _record_failure(name: str, exc: BaseException) -> None:
    """Report one per-instance failure inside an --all loop (never raises).

    String SystemExit codes (e.g. compose errors) carry the message; integer
    codes (e.g. an unverified status) were already reported inline.
    """
    if isinstance(exc, SystemExit):
        if isinstance(exc.code, str) and exc.code:
            click.echo(f"{name}: {exc.code}", err=True)
    elif isinstance(exc, click.ClickException):
        click.echo(f"{name}: Error: {exc.message}", err=True)
    else:
        click.echo(f"{name}: Error: {exc}", err=True)


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
    return _runtime_selection_or_error()[0]


def _runtime_selection_or_error() -> tuple[Selection | None, bool]:
    """(selection, reachable): the live selection, plus whether the control
    server responded at all (None/-False when unreachable or blank)."""
    try:
        return Selection.from_doc(control.get_settings()), True
    except control.ControlError:
        return None, False


def _require_selection() -> Selection:
    sel = effective_selection()
    if sel is None or not sel.provider:
        raise click.ClickException(
            "Cannot read runtime settings — is the control server reachable?"
        )
    return sel


def _baked_selection() -> Selection | None:
    """Selection baked into the container at create time (its compose env)."""
    env = container_env()
    provider = env.get("VPN_SERVICE_PROVIDER")
    if not provider:
        return None
    return Selection(
        provider,
        env.get("VPN_TYPE") or "",
        env.get("SERVER_COUNTRIES") or env.get("VPN_COUNTRY"),
        env.get("SERVER_CITIES") or env.get("VPN_CITY"),
    )


def _status_doc() -> dict[str, object]:
    """Stable status document (status --json). Called inside the instance context.

    Contract (README §"Machine-readable output"): exit code is 0 for a healthy
    or simply-stopped instance, 1 when the tunnel verifiably leaks or the
    control server is unreachable while the container runs/restarts. Probe
    failure is probe health, never a leak: it is reported in ``last_error``
    with ``leak: false`` and exit 0 (C8).
    """
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

    exit_ip: dict[str, str | None] | None = None
    leak = False
    verified = False
    if state == "running":
        result = _probe()
        if result is None:
            if last_error is None:
                last_error = "could not determine the exit IP (all echo services failed)"
        else:
            info = result.info
            ip = str(info.get("ip") or "")
            if not ip:
                if last_error is None:
                    last_error = "could not determine the exit IP (empty observation)"
            else:
                exit_ip = {
                    "ip": ip,
                    "country": _clean_country(info.get("country")),
                }
                verdict, verified = classify_verdict(real_ip(), ip)
                leak = verdict == "leak"
                if verdict == "unknown" and last_error is None:
                    last_error = (
                        "could not determine the host's bare IP — tunnel unverified"
                    )
    return {
        "instance": inst.name,
        "container_name": inst.name,
        "image": container_image() if state != "absent" else None,
        "state": state,
        "selection": selection_doc(sel) if sel and sel.provider else None,
        "drift": drift,
        "control_server": control_server_doc(inst.control_port, enabled),
        "exit_ip": exit_ip,
        "leak": leak,
        "verified": verified,
        "last_error": last_error,
    }


def _clean_country(value: object) -> str | None:
    """Normalize a raw country field: missing/''/'None'/'null' become None (C3)."""
    text = str(value or "").strip()
    if not text or text.lower() in ("none", "null"):
        return None
    return resolve_country(text) or None


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
        raise click.UsageError(
            "--city needs a country to search within: pass --country, or connect "
            "to a country first."
        )

    target_provider, target_protocol = validate_provider(target_provider, target_protocol)
    target = Selection(target_provider, target_protocol, target_country, target_city)
    if target.key == base.key:
        return target, False
    try:
        apply_location(target)
    except control.ControlError as exc:
        raise click.ClickException(
            f"Could not switch to {_print_target(target)} — {exc.message}"
        ) from exc
    return target, True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version=__version__, prog_name="epoxy", message="%(prog)s %(version)s")
@click.option("--debug", is_flag=True, envvar="EPOXY_DEBUG", help="Enable debug output")
def main(debug: bool) -> None:
    """Epoxy VPN manager."""
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
@click.option("--pull", is_flag=True, help="Pull the latest container image first")
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
    # A fresh, registry-less instance gets a free host port allocated and
    # persisted, so its registry survives restarts without ever colliding with
    # another instance. A registry-less but running instance instead adopts its
    # published control port, so an imported/legacy container stays addressable
    # even though its registry record is gone.
    if (
        ctl_port is None
        and inst.env.get("EPOXY_CTL_PORT") is None
        and read_registry(inst.name) is None
    ):
        if container_running(name=inst.name):
            published = container_control_port(name=inst.name)
            if published is not None and published != inst.control_port:
                inst = replace(inst, control_port=published)
        else:
            inst = replace(inst, control_port=allocate_free_port())
    ensure_compose_file(inst)
    with instance_context(inst):
        require_api_key()
        requested = any(v is not None for v in (provider, protocol, country, city))
        was_running = container_running()
        current = effective_selection() if was_running else None
        created = not was_running
        if pull:
            run("docker", "pull", ENGINE_IMAGE, timeout=PULL_TIMEOUT_S)
            recreate = True
        if recreate:
            created = True

        # On a create/recreate path compose already applies provider/protocol;
        # only an explicit location constitutes a further hot-swap request there.
        if created:
            requested = country is not None or city is not None
        if was_running and requested and current is None and not recreate:
            raise click.ClickException(
                "Cannot read runtime settings — is the control server reachable?"
            )

        swapped = False
        target = Selection("", "")
        prev_ip: str | None = None
        if created:
            name = provider or (current.provider if current else None)
            if not name:
                raise click.UsageError("--provider is required to start the container.")
            name, proto = resolve_provider(name, protocol, current.protocol if current else None)
            overrides = get_provider_env(name, proto)
            _log_env(overrides)
            compose(
                "up",
                "-d",
                *(("--force-recreate",) if recreate else ()),
                env_overrides=overrides,
                timeout=COMPOSE_TIMEOUT_S,
            )
            write_registry(inst)
            click.echo(f"VPN {'recreated' if recreate else 'started'} ({name}/{proto}).")
            # Runtime state now equals env config: the fresh container runs the
            # baked pair (provider, protocol, and any baked country/city), so
            # requests resolve against that instead of an unfilled Selection.
            current = _baked_selection() or Selection(name, proto)

        if requested:
            if not created:
                # A hot-swap on a live tunnel must move the exit off this IP.
                prev_ip = current_exit_ip()
            else:
                # A fresh container's control server is usually not listening
                # yet — wait for it instead of failing the first GET. A still
                # unreachable server falls through to _apply_request, which
                # reports the friendly "Could not switch to ..." error.
                click.echo("Waiting for control server...")
                with contextlib.suppress(control.ControlError):
                    control.wait_for_settings()
            base = current or Selection("", "")
            target, swapped = _apply_request(provider, protocol, country, city, base)
            click.echo(f"{'Swapped to' if swapped else 'Already on'} {_print_target(target)}.")
        elif current is not None:
            target = current

        verified = finish_connection(
            expected_country=target.country or None,
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
            raise click.ClickException("No servers found. Is Docker running?")
        print_servers_table(by_provider)
        return
    inst = _resolve_for_command(instance)
    with instance_context(inst):
        require_api_key()
        if not container_running():
            raise click.ClickException(
                f"Container '{current_instance().container}' is not running. Use 'epoxy up' first."
            )
        current = _require_selection()

        if not any(v is not None for v in (provider, protocol, country, city)):
            by_provider = listable_servers(get_servers())
            if not any(by_provider.values()):
                raise click.ClickException("No servers found. Is Docker running?")
            selection = select_server(by_provider)
            if not selection:
                raise click.ClickException("No selection.")
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
@all_option
def down(instance: str | None, all_instances: bool) -> None:
    """Stop the VPN container."""
    targets = _resolve_targets(instance, all_instances)
    if not targets:
        click.echo("(no instances)")
        return
    failures = 0
    for inst in targets:
        with instance_context(inst):
            try:
                with contextlib.suppress(Exception):
                    control.set_tunnel_status("stopped", timeout=DOWN_TIMEOUT_S)
                compose("down", timeout=COMPOSE_TIMEOUT_S)
            except (Exception, SystemExit) as exc:  # per-instance: report and continue
                _record_failure(inst.name, exc)
                failures += 1
                continue
            click.echo(f"{inst.name}: VPN stopped." if all_instances else "VPN stopped.")
    if failures:
        raise SystemExit(1)


def _remove_one(inst: Instance, force: bool) -> None:
    """Remove one resolved instance: guard, stop, compose down, delete state."""
    name = inst.name
    if name not in discovery._known_names():
        raise click.ClickException(f"Unknown instance '{name}'.")
    consumers = discovery.consumers_of(name)
    if consumers and not force:
        raise click.ClickException(
            f"Instance '{name}' is shared by: {', '.join(consumers)}. "
            "Re-run with --force to remove it anyway."
        )
    with instance_context(inst):
        with contextlib.suppress(Exception):
            control.set_tunnel_status("stopped", timeout=DOWN_TIMEOUT_S)
        if Path(inst.compose_file).exists():
            compose("down", timeout=COMPOSE_TIMEOUT_S)
        else:
            remove_container(name)
    delete_instance_state(name)
    click.echo(f"Instance '{name}' removed.")


@main.command()
@add_instance_options()
@all_option
@click.option(
    "-f",
    "--force",
    is_flag=True,
    help="Remove even when consumers share the instance's network",
)
def rm(instance: str | None, force: bool, all_instances: bool) -> None:
    """Remove the instance's container and delete its registry state."""
    targets = _resolve_targets(instance, all_instances)
    if not all_instances:
        _remove_one(targets[0], force)
        return
    if not targets:
        click.echo("(no instances)")
        return
    failures = 0
    for inst in targets:
        try:
            _remove_one(inst, force)
        except (Exception, SystemExit) as exc:  # per-instance: report and continue
            _record_failure(inst.name, exc)
            failures += 1
    if failures:
        raise SystemExit(1)


@main.command()
@add_instance_options()
@all_option
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
@click.option("-n", "--tail", default="50", help="Number of lines to show")
def logs(instance: str | None, all_instances: bool, follow: bool, tail: str) -> None:
    """Show container logs."""
    if all_instances and follow:
        raise click.UsageError("--follow cannot be used with --all.")
    targets = _resolve_targets(instance, all_instances)
    if not targets:
        click.echo("(no instances)")
        return
    failures = 0
    for inst in targets:
        with instance_context(inst):
            try:
                args: list[str] = ["logs"]
                if follow:
                    args.append("-f")
                args.extend(["--tail", tail, current_instance().container])
                if all_instances:
                    click.echo(f"== {inst.name} ==")
                compose(*args)
            except (Exception, SystemExit) as exc:  # per-instance: report and continue
                _record_failure(inst.name, exc)
                failures += 1
    if failures:
        raise SystemExit(1)


def _kv(label: str, value: str, color: str | None = None) -> None:
    """Print an indented key-value line with a bold label."""
    text = f"  {label:<12}{value}"
    if color:
        click.echo(click.style(text, fg=color))
    else:
        click.echo(text)


def _status_json_failed(doc: dict[str, object]) -> bool:
    """Whether a status document trips the exit-1 rules: leak, or unreachable
    control server while the container runs/restarts."""
    if doc["leak"]:
        return True
    control_doc = doc["control_server"]
    return (
        doc["state"] in ("running", "starting")
        and isinstance(control_doc, dict)
        and not control_doc.get("enabled")
        and bool(doc["last_error"])
    )


def _print_human_status(size: int, no_speedtest: bool) -> None:
    """Human status body for the active instance (raises on failure, as before)."""
    state = container_status()
    if not state:
        click.echo(f"Container '{current_instance().container}' not found.")
        raise click.ClickException(f"Container '{current_instance().container}' is not running.")

    container = current_instance().container
    _kv("Container", f"{container} ({state})")

    try:
        tunnel = control.get_tunnel_status()
        _kv("Tunnel", tunnel, "red" if tunnel == "stopped" else None)
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

    current, reachable = _runtime_selection_or_error()
    if current and current.provider:
        click.echo()
        _kv("Provider", current.provider)
        _kv("Protocol", current.protocol or "?")
        if current.country:
            loc = ", ".join(filter(None, [current.city, current.country]))
            _kv("Location", loc)
    else:
        click.echo()
        _kv("Provider", "unknown — is the control server reachable?")

    if state == "running":
        click.echo()
        verified = finish_connection(
            expected_country=current.country if current and current.country else None,
            speedtest=not no_speedtest,
            size=size,
        )
        if not verified:
            raise SystemExit(1)
    if state in ("running", "starting") and not reachable:
        raise SystemExit(1)


@main.command()
@add_instance_options()
@all_option
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
    instance: str | None, size: int, no_speedtest: bool, json_output: bool, all_instances: bool
) -> None:
    """Show container state, effective selection, public IP, and speed test.

    Exit codes (M9): 0 healthy/stopped · 1 leak, unreachable control server
    (while the container runs/restarts), or verification failure (human mode)
    · 2 usage. ``--json`` exits 0 on an inconclusive probe — probe health is
    reported in ``last_error``, never conflated with a leak. With ``--all``,
    failures are reported per instance and the exit code reflects the worst
    one; ``--json`` emits an ``{"instances": [...]}`` envelope.
    """
    targets = _resolve_targets(instance, all_instances)
    if all_instances:
        if json_output:
            docs = []
            for inst in targets:
                with instance_context(inst):
                    docs.append(_status_doc())
            click.echo(json.dumps({"instances": docs}))
            if any(_status_json_failed(doc) for doc in docs):
                raise SystemExit(1)
            return
        if not targets:
            click.echo("(no instances)")
            return
        failures = 0
        for index, inst in enumerate(targets):
            if index:
                click.echo()
            click.echo(f"== {inst.name} ==")
            with instance_context(inst):
                try:
                    _print_human_status(size, no_speedtest)
                except (Exception, SystemExit) as exc:  # per-instance: report and continue
                    _record_failure(inst.name, exc)
                    failures += 1
        if failures:
            raise SystemExit(1)
        return
    with instance_context(_resolve_for_command(instance)):
        if json_output:
            doc = _status_doc()
            click.echo(json.dumps(doc))
            if _status_json_failed(doc):
                raise SystemExit(1)
            return
        _print_human_status(size, no_speedtest)


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
            raise click.ClickException(
                f"Container '{current_instance().container}' is not running."
            )
        by_provider = listable_servers(get_servers())
        if not any(by_provider.values()):
            raise click.ClickException("No servers found. Is Docker running?")

        try:
            control.get_settings()
        except control.ControlError as exc:
            raise click.ClickException(
                f"Cannot reach the control server: {exc.message}"
            ) from None

        candidates = build_candidates(by_provider, provider, protocol, country)
        if not candidates:
            raise click.ClickException("No matching locations for the given filters.")

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
        except control.ControlError as exc:
            raise click.ClickException(
                f"Cannot reach the control server: {exc.message}"
            ) from None

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
    """List instances (registry and epoxy-* compose containers) and their consumers."""
    records = instance_records()
    if instance:
        records = [r for r in records if r["instance"] == instance]
    if json_output:
        click.echo(json.dumps({"instances": records}))
        return
    print_ls_table(records)


@main.command()
@add_instance_options()
@all_option
@click.argument("action", required=False, type=click.Choice(["on", "off"]))
def dns(instance: str | None, all_instances: bool, action: str | None) -> None:
    """Show or toggle the DNS-over-TLS resolver."""
    targets = _resolve_targets(instance, all_instances)
    if not targets:
        click.echo("(no instances)")
        return
    failures = 0
    for inst in targets:
        with instance_context(inst):
            try:
                if action is None:
                    try:
                        dns_status = control.get_dns_status()
                    except control.ControlError as exc:
                        raise click.ClickException(
                            f"Cannot reach control server: {exc.message}"
                        ) from None
                    click.echo(
                        f"{inst.name}: DNS: {dns_status}" if all_instances else f"DNS: {dns_status}"
                    )
                else:
                    target = "running" if action == "on" else "stopped"
                    try:
                        control.set_dns_status(target)
                    except control.ControlError as exc:
                        raise click.ClickException(
                            f"Cannot reach control server: {exc.message}"
                        ) from None
                    click.echo(
                        f"{inst.name}: DNS {target}." if all_instances else f"DNS {target}."
                    )
            except (Exception, SystemExit) as exc:  # per-instance: report and continue
                _record_failure(inst.name, exc)
                failures += 1
    if failures:
        raise SystemExit(1)


@main.command()
@add_instance_options()
@all_option
def update(instance: str | None, all_instances: bool) -> None:
    """Trigger a server list update."""
    targets = _resolve_targets(instance, all_instances)
    if not targets:
        click.echo("(no instances)")
        return
    failures = 0
    for inst in targets:
        with instance_context(inst):
            try:
                try:
                    control.trigger_updater()
                except control.ControlError as exc:
                    raise click.ClickException(
                        f"Cannot reach control server: {exc.message}"
                    ) from None
                click.echo(
                    f"{inst.name}: Server list update triggered."
                    if all_instances
                    else "Server list update triggered."
                )
            except (Exception, SystemExit) as exc:  # per-instance: report and continue
                _record_failure(inst.name, exc)
                failures += 1
    if failures:
        raise SystemExit(1)
