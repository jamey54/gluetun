"""Stop the VPN container."""

import contextlib

import click

from epoxy import control, docker
from epoxy.commands._common import (
    _resolve_targets,
    add_instance_options,
    all_option,
    for_each_instance,
)
from epoxy.config import COMPOSE_TIMEOUT_S, DOWN_TIMEOUT_S
from epoxy.instance import Instance, instance_context


@click.command()
@add_instance_options()
@all_option
def down(instance: str | None, all_instances: bool) -> None:
    """Stop the VPN container."""
    targets = _resolve_targets(instance, all_instances)

    def stop_one(inst: Instance, fan_out: bool) -> None:
        with instance_context(inst):
            with contextlib.suppress(Exception):
                control.set_tunnel_status("stopped", timeout=DOWN_TIMEOUT_S)
            docker.compose("down", timeout=COMPOSE_TIMEOUT_S)
        click.echo(f"{inst.name}: VPN stopped." if fan_out else "VPN stopped.")

    for_each_instance(targets, stop_one, all_instances)
