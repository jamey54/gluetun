"""Remove the instance's container and delete its registry state."""

import contextlib
from pathlib import Path

import click

from epoxy import control, discovery, docker
from epoxy.commands._common import (
    _resolve_targets,
    add_instance_options,
    all_option,
    for_each_instance,
)
from epoxy.config import COMPOSE_TIMEOUT_S, DOWN_TIMEOUT_S
from epoxy.instance import Instance, delete_instance_state, instance_context


def _remove_one(inst: Instance, force: bool) -> None:
    """Remove one resolved instance: guard, stop, compose down, delete state."""
    name = inst.name
    if name not in discovery.known_names():
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
            docker.compose("down", timeout=COMPOSE_TIMEOUT_S)
        else:
            docker.remove_container(name)
    delete_instance_state(name)
    click.echo(f"Instance '{name}' removed.")


@click.command()
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

    def remove_one(inst: Instance, _fan_out: bool) -> None:
        _remove_one(inst, force)

    for_each_instance(targets, remove_one, all_instances)
