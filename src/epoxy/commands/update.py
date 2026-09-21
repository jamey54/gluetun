"""Trigger a server list update."""

import click

from epoxy import control
from epoxy.commands._common import (
    _resolve_targets,
    add_instance_options,
    all_option,
    for_each_instance,
)
from epoxy.instance import Instance, instance_context


@click.command()
@add_instance_options()
@all_option
def update(instance: str | None, all_instances: bool) -> None:
    """Trigger a server list update."""
    targets = _resolve_targets(instance, all_instances)

    def update_one(inst: Instance, fan_out: bool) -> None:
        with instance_context(inst):
            try:
                control.trigger_updater()
            except control.ControlError as exc:
                raise click.ClickException(f"Cannot reach control server: {exc.message}") from exc
            prefix = f"{inst.name}: " if fan_out else ""
            click.echo(f"{prefix}Server list update triggered.")

    for_each_instance(targets, update_one, all_instances)
