"""Shared CLI plumbing: instance resolution, runtime helpers, fan-out."""

import os
import sys
from collections.abc import Callable
from dataclasses import replace
from typing import Any, NoReturn, TypeVar

import click

from epoxy import apply, control, discovery, docker, ipinfo, picker, speedtest
from epoxy.apply import Selection
from epoxy.config import (
    CTL_PORT_ENV_VAR,
    DEFAULT_PROTOCOL,
    DEFAULT_SIZE_MB,
    INSTANCE_ENV_VAR,
)
from epoxy.instance import (
    Instance,
    current_instance,
    env_lookup,
    read_registry,
    resolve_instance,
)
from epoxy.providers import PROVIDERS, validate_provider
from epoxy.textutil import fold

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
            help=f"Instance name (default: ${INSTANCE_ENV_VAR}; required when unset)",
        )(func)
        return func

    return decorate


def _no_instance_error() -> NoReturn:
    """The documented no-target error: --instance or EPOXY_INSTANCE required."""
    raise click.UsageError(f"No instance selected: pass --instance or set {INSTANCE_ENV_VAR}.")


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
    chosen = picker.select_instance([(name, discovery._state(name)) for name in names])
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
        env_port = base.env.get(CTL_PORT_ENV_VAR)
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                raise click.UsageError(
                    f"{CTL_PORT_ENV_VAR} must be a port number, got {env_port!r}"
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
        published = docker.container_control_port(name=base.name)
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


def for_each_instance(
    targets: list[Instance],
    action: Callable[[Instance, bool], None],
    all_instances: bool,
    header: bool = False,
    separate: bool = False,
) -> None:
    """Run an action for one instance or every known instance, with isolation.

    Empty targets print ``(no instances)``; per-instance failures are reported
    via ``_record_failure`` and the run continues, exiting ``1`` when any
    instance failed. ``header`` prints a ``== <name> ==`` banner per instance
    under ``--all`` (``separate`` adds a blank line between instances).
    """
    if not targets:
        click.echo("(no instances)")
        return
    failures = 0
    for index, inst in enumerate(targets):
        if header and all_instances:
            if separate and index:
                click.echo()
            click.echo(f"== {inst.name} ==")
        try:
            action(inst, all_instances)
        except (Exception, SystemExit) as exc:  # per-instance: report and continue
            _record_failure(inst.name, exc)
            failures += 1
    if failures:
        raise SystemExit(1)


def finish_connection(
    expected_country: str | None = None,
    run_speedtest: bool = True,
    size: int = DEFAULT_SIZE_MB,
    exclude_ips: set[str] | None = None,
) -> bool:
    """Show connection status and optionally run a speed test."""
    verified = ipinfo.print_ip_status(expected_country=expected_country, exclude_ips=exclude_ips)
    if verified and run_speedtest:
        click.echo("Running speed test...")
        result = speedtest.measure(size)
        if result:
            click.echo(speedtest.format_result(result))
        else:
            click.echo("Speed test failed.")
    elif run_speedtest:
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
    env = docker.container_env()
    provider = env.get("VPN_SERVICE_PROVIDER")
    if not provider:
        return None
    return Selection(
        provider,
        env.get("VPN_TYPE") or "",
        env.get("SERVER_COUNTRIES") or env.get("VPN_COUNTRY"),
        env.get("SERVER_CITIES") or env.get("VPN_CITY"),
    )


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
        apply.apply_location(target)
    except control.ControlError as exc:
        raise click.ClickException(
            f"Could not switch to {_print_target(target)} — {exc.message}"
        ) from exc
    return target, True
