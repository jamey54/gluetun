"""Show container logs."""

import click

from epoxy import docker
from epoxy.commands._common import (
    _resolve_targets,
    add_instance_options,
    all_option,
    for_each_instance,
)
from epoxy.instance import COMPOSE_SERVICE, Instance, instance_context


@click.command()
@add_instance_options()
@all_option
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
@click.option("-n", "--tail", default="50", help="Number of lines to show")
def logs(instance: str | None, all_instances: bool, follow: bool, tail: str) -> None:
    """Show container logs."""
    if all_instances and follow:
        raise click.UsageError("--follow cannot be used with --all.")
    targets = _resolve_targets(instance, all_instances)

    def show_one(inst: Instance, _fan_out: bool) -> None:
        with instance_context(inst):
            args: list[str] = ["logs"]
            if follow:
                args.append("-f")
            # `docker compose logs` takes a SERVICE name, which the template fixes
            # at COMPOSE_SERVICE for every instance -- not the container name,
            # which is only `epoxy` for the instance of that name.
            args.extend(["--tail", tail, COMPOSE_SERVICE])
            docker.compose(*args)

    for_each_instance(targets, show_one, all_instances, header=True)
