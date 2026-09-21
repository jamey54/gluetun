"""Show or toggle the DNS-over-TLS resolver."""

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
@click.argument("action", required=False, type=click.Choice(["on", "off"]))
def dns(instance: str | None, all_instances: bool, action: str | None) -> None:
    """Show or toggle the DNS-over-TLS resolver."""
    targets = _resolve_targets(instance, all_instances)

    def dns_one(inst: Instance, fan_out: bool) -> None:
        with instance_context(inst):
            try:
                if action is None:
                    text = f"DNS: {control.get_dns_status()}"
                else:
                    target = "running" if action == "on" else "stopped"
                    control.set_dns_status(target)
                    text = f"DNS {target}."
            except control.ControlError as exc:
                raise click.ClickException(f"Cannot reach control server: {exc.message}") from exc
            prefix = f"{inst.name}: " if fan_out else ""
            click.echo(prefix + text)

    for_each_instance(targets, dns_one, all_instances)
