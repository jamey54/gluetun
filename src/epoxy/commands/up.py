"""Start (or verify) the VPN; apply any requested location via hot-swap."""

import contextlib
from dataclasses import replace

import click

from epoxy import control, docker, ipinfo
from epoxy.apply import Selection
from epoxy.commands import _common
from epoxy.commands._common import (
    PROTOCOL,
    _apply_request,
    _baked_selection,
    _log_env,
    _print_target,
    _resolve_for_command,
    add_instance_options,
)
from epoxy.config import COMPOSE_TIMEOUT_S, CTL_PORT_ENV_VAR, PULL_TIMEOUT_S, image_ref
from epoxy.instance import (
    allocate_free_port,
    ensure_compose_file,
    instance_context,
    read_registry,
    sync_registry,
    write_registry,
)
from epoxy.providers import get_provider_env, resolve_provider


@click.command()
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
@click.option("--pull", is_flag=True, help="Pull the pinned container image first")
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
        and inst.env.get(CTL_PORT_ENV_VAR) is None
        and read_registry(inst.name) is None
    ):
        if docker.container_running(name=inst.name):
            published = docker.container_control_port(name=inst.name)
            if published is not None and published != inst.control_port:
                inst = replace(inst, control_port=published)
        else:
            inst = replace(inst, control_port=allocate_free_port())
    ensure_compose_file(inst)
    with instance_context(inst):
        _common.require_api_key()
        requested = any(v is not None for v in (provider, protocol, country, city))
        was_running = docker.container_running()
        current = _common.effective_selection() if was_running else None
        created = not was_running
        if pull:
            docker.run("docker", "pull", image_ref(), timeout=PULL_TIMEOUT_S)
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
            docker.compose(
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
                prev_ip = ipinfo.current_exit_ip()
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

        verified = _common.finish_connection(
            expected_country=target.country or None,
            run_speedtest=not no_speedtest,
            exclude_ips={prev_ip} if (prev_ip and swapped) else None,
        )
        # The compose file above was written with the resolved port, so keep the
        # registry in step with it — an explicit --ctl-port must not be lost to a
        # stale record on the next command.
        sync_registry(inst)
    if not verified:
        raise SystemExit(1)
