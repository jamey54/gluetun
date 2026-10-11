"""Start (or verify) the VPN; apply any requested location via hot-swap."""

import contextlib
from dataclasses import replace

import click

from epoxy import control, docker, ipinfo
from epoxy.apply import Selection, try_swap_lock
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
from epoxy.config import COMPOSE_TIMEOUT_S, CTL_PORT_ENV_VAR, PULL_TIMEOUT_S
from epoxy.instance import (
    Instance,
    allocate_free_port,
    ensure_compose_file,
    host_port_held,
    image_ref,
    instance_context,
    list_registry,
    read_registry,
    sync_registry,
    write_registry,
)
from epoxy.listeners import describe_holder
from epoxy.providers import get_provider_env, resolve_provider
from epoxy.publish import canonical_spec, parse_publish_spec, spec_host_port


def _parse_specs(specs: tuple[str, ...], flag: str = "--publish") -> dict[int, str]:
    """Canonical specs by host port; conflicting duplicates are usage errors."""
    by_host: dict[int, str] = {}
    for spec in specs:
        bind_ip, host_port, container_port = parse_publish_spec(spec)
        canonical = canonical_spec(bind_ip, host_port, container_port)
        if host_port in by_host and by_host[host_port] != canonical:
            raise click.UsageError(f"Conflicting {flag} specs for host port {host_port}.")
        by_host[host_port] = canonical
    return by_host


def _stored_forwarded(name: str) -> tuple[str, ...]:
    """Stored forward set; empty for missing records or legacy/garbled ones."""
    record = read_registry(name)
    raw = record.get("forwarded_ports") if record else None
    if isinstance(raw, list) and all(isinstance(spec, str) for spec in raw):
        return tuple(raw)
    return ()


def _resolve_forwarded(
    inst: Instance,
    publish_specs: tuple[str, ...],
    append_specs: tuple[str, ...],
    no_publish: bool,
) -> tuple[str, ...]:
    """Effective forwarded set.

    ``--publish`` replaces, ``--append-publish`` unions, ``--no-publish``
    clears, omitting all three preserves. Only one mode per invocation;
    malformed specs and conflicting host ports are usage errors (exit 2).
    """
    modes = bool(publish_specs) + bool(append_specs) + bool(no_publish)
    if modes > 1:
        if publish_specs and no_publish:
            raise click.UsageError("--publish and --no-publish are mutually exclusive.")
        raise click.UsageError(
            "--append-publish cannot be combined with --publish or --no-publish."
        )
    if no_publish:
        return ()
    if append_specs:
        merged = _parse_specs(append_specs, "--append-publish")
        for spec in inst.forwarded_ports:
            host = spec_host_port(spec)
            if host is None:
                continue
            if host in merged and merged[host] != spec:
                raise click.UsageError(
                    f"Host port {host} is already forwarded to {spec}; "
                    "use --publish to change or replace it."
                )
            merged.setdefault(host, spec)
        return tuple(sorted(merged.values()))
    if not publish_specs:
        return tuple(inst.forwarded_ports)
    return tuple(sorted(_parse_specs(publish_specs).values()))


def _locked_create(
    inst: Instance,
    overrides: dict[str, str],
    recreate: bool,
    announce_publish_recreate: bool,
    publish_specs: tuple[str, ...],
    append_specs: tuple[str, ...],
    no_publish: bool,
    name: str,
    proto: str,
) -> tuple[Instance, tuple[str, ...]]:
    """Merge, validate, render, compose, and persist under the instance lock.

    Re-reading the registry inside the lock is what makes concurrent appends
    union instead of clobber. Fails fast with a retry-later error when another
    operation holds the lock (a bench can hold it for minutes); the slow tail
    (control-server wait, speedtest) stays outside the lock.
    """
    with try_swap_lock() as acquired:
        if not acquired:
            raise click.ClickException(
                f"Instance '{inst.name}' is busy (another operation holds it); retry later."
            )
        if announce_publish_recreate:
            click.echo(
                "Published ports changed — recreating container "
                "(runtime selection reverts to baked config)."
            )
        merged = _resolve_forwarded(
            replace(inst, forwarded_ports=_stored_forwarded(inst.name)),
            publish_specs,
            append_specs,
            no_publish,
        )
        merged_inst = replace(inst, forwarded_ports=merged)
        _check_forward_collisions(merged_inst, merged)
        ensure_compose_file(merged_inst)
        docker.compose(
            "up",
            "-d",
            *(("--force-recreate",) if recreate else ()),
            env_overrides=overrides,
            timeout=COMPOSE_TIMEOUT_S,
        )
        write_registry(merged_inst)
        click.echo(f"VPN {'recreated' if recreate else 'started'} ({name}/{proto}).")
        if merged:
            click.echo(f"Forwarded ports: {', '.join(merged)}.")
        return merged_inst, merged


def _check_forward_collisions(inst: Instance, effective: tuple[str, ...]) -> None:
    """Refuse an unusable forward set before compose touches anything.

    Control-port clashes are usage errors (exit 2); ports claimed by another
    instance or held live are operational failures (exit 1) naming the port
    and, when known, its holder. The instance's own live ports are exempt:
    recreating frees them.
    """
    if not effective:
        return
    hosts = [host for spec in effective if (host := spec_host_port(spec)) is not None]
    if inst.control_port in hosts:
        raise click.UsageError(
            f"--publish host port {inst.control_port} collides with the instance's control port."
        )
    for name in sorted(list_registry()):
        if name == inst.name:
            continue
        record = read_registry(name)
        if not record:
            continue
        held = _record_claims(name, record, hosts)
        if held:
            raise click.ClickException("; ".join(held))
    live = docker.published_host_ports(name=inst.name)
    for spec in effective:
        bind_ip, host_port, _ = parse_publish_spec(spec)  # validated above
        if host_port in live:
            continue
        if host_port_held(bind_ip, host_port):
            detail = describe_holder(host_port, bind_ip)
            suffix = f" (held by {detail})" if detail else ""
            raise click.ClickException(f"Host port {host_port} is already in use{suffix}.")


def _record_claims(name: str, record: dict[str, object], hosts: list[int]) -> list[str]:
    """Per-port conflict descriptions against one registry record."""
    claims: list[str] = []
    port = record.get("control_port")
    forwards = record.get("forwarded_ports")
    forward_hosts: set[int] = set()
    if isinstance(forwards, list):
        forward_hosts = {
            host
            for spec in forwards
            if isinstance(spec, str) and (host := spec_host_port(spec)) is not None
        }
    for host in sorted(set(hosts)):
        if isinstance(port, int) and host == port:
            claims.append(f"Host port {host} is the control port of instance '{name}'.")
        elif host in forward_hosts:
            claims.append(f"Host port {host} is already forwarded by instance '{name}'.")
    return claims


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
@click.option(
    "--publish",
    "publish_specs",
    multiple=True,
    help="Forward a host port into the instance ([IP:]HOST:CONTAINER, "
    "repeatable; replaces the stored set on recreate)",
)
@click.option(
    "--append-publish",
    "append_specs",
    multiple=True,
    help="Forward a host port without replacing the stored set "
    "([IP:]HOST:CONTAINER, repeatable; unioned atomically under the instance lock)",
)
@click.option("--no-publish", is_flag=True, help="Clear all forwarded ports (applied on recreate)")
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
    publish_specs: tuple[str, ...],
    append_specs: tuple[str, ...],
    no_publish: bool,
) -> None:
    """Start the VPN; apply any requested location via hot-swap.

    With no arguments on a running container this only verifies the tunnel.
    Selections are runtime-only: --pull/--recreate revert to compose/.env config.
    """
    inst = _resolve_for_command(instance, ctl_port, env_file)
    # Forwarded ports are instance config: --publish replaces the stored set,
    # --append-publish unions into it, --no-publish clears it, omitting all
    # three preserves it.
    publish_flags = bool(publish_specs or append_specs or no_publish)
    effective_forwards = _resolve_forwarded(inst, publish_specs, append_specs, no_publish)
    previous_forwards = set(inst.forwarded_ports)
    if set(effective_forwards) != previous_forwards:
        inst = replace(inst, forwarded_ports=effective_forwards)
    # A fresh, registry-less instance gets a free host port allocated and
    # persisted, so its registry survives restarts without ever colliding with
    # another instance. A registry-less but running instance instead adopts its
    # published control port, so an imported/legacy container stays addressable
    # even though its registry record is gone.
    # EPOXY_CTL_PORT is tested for truthiness, matching how _resolve_for_command
    # reads it: an empty value means unset, so it must not suppress allocation
    # (that would strand the instance on the hardcoded BASE_CONTROL_PORT).
    if ctl_port is None and not inst.env.get(CTL_PORT_ENV_VAR) and read_registry(inst.name) is None:
        if docker.container_running(name=inst.name):
            published = docker.container_control_port(name=inst.name)
            if published is not None and published != inst.control_port:
                inst = replace(inst, control_port=published)
        else:
            inst = replace(
                inst,
                control_port=allocate_free_port(
                    exclude=frozenset(
                        host
                        for spec in effective_forwards
                        if (host := spec_host_port(spec)) is not None
                    )
                ),
            )
    with instance_context(inst):
        # Rendering resolves image_ref() through the active instance's env, so
        # the compose file must be written inside the context: outside of it
        # current_instance() falls back to default_instance(), which is a usage
        # error in production.
        ensure_compose_file(inst)
        _common.require_api_key()
        requested = any(v is not None for v in (provider, protocol, country, city))
        was_running = docker.container_running()
        current = _common.effective_selection() if was_running else None
        created = not was_running
        if pull:
            docker.run("docker", "pull", image_ref(), timeout=PULL_TIMEOUT_S)
            recreate = True
        announce_publish_recreate = False
        if (
            publish_flags
            and set(effective_forwards) != previous_forwards
            and was_running
            and not recreate
        ):
            # Ports only take effect at (re)create, like --pull: converge with
            # a loud echo rather than a two-step dance. Runtime selection
            # reverts to baked config, hence the warning. The echo itself
            # fires inside the locked section below, after acquiring the lock.
            recreate = True
            announce_publish_recreate = True
        if recreate:
            created = True

        # On a create/recreate path compose already applies provider/protocol;
        # only an explicit location constitutes a further hot-swap request there.
        if created:
            requested = country is not None or city is not None
        if was_running and not recreate and current is None:
            # Running but unreadable is not yet broken: the control server may
            # still be booting (e.g. mid-restart), so wait before giving up.
            current = _common.await_selection()
        if was_running and not recreate and current is None:
            # Mirrors `status`: a running instance whose control server never
            # answers is an error, whether or not a swap was requested.
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
            if publish_flags:
                inst, effective_forwards = _locked_create(
                    inst,
                    overrides,
                    recreate,
                    announce_publish_recreate,
                    publish_specs,
                    append_specs,
                    no_publish,
                    name,
                    proto,
                )
            else:
                _check_forward_collisions(inst, effective_forwards)
                docker.compose(
                    "up",
                    "-d",
                    *(("--force-recreate",) if recreate else ()),
                    env_overrides=overrides,
                    timeout=COMPOSE_TIMEOUT_S,
                )
                write_registry(inst)
                click.echo(f"VPN {'recreated' if recreate else 'started'} ({name}/{proto}).")
                if effective_forwards:
                    click.echo(f"Forwarded ports: {', '.join(effective_forwards)}.")
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
