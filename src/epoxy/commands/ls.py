"""List instances and their consumers."""

import json

import click

from epoxy import discovery
from epoxy.commands._common import add_instance_options


@click.command()
@add_instance_options()
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Machine-readable list (single-line JSON)",
)
def ls(instance: str | None, json_output: bool) -> None:
    """List instances (registry and epoxy-* compose containers) and their consumers."""
    records = discovery.instance_records()
    if instance:
        records = [r for r in records if r["instance"] == instance]
    if json_output:
        click.echo(json.dumps({"instances": records}))
        return
    discovery.print_ls_table(records)
