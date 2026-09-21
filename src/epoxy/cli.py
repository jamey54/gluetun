"""Epoxy CLI entry point: command group, shared flags, and command wiring."""

import click

from epoxy.commands import (
    _common,
    bench,
    connect,
    dns,
    down,
    install,
    logs,
    ls,
    rm,
    status,
    up,
    update,
)
from epoxy.config import DEBUG_ENV_VAR
from epoxy.version import __version__


@click.group()
@click.version_option(version=__version__, prog_name="epoxy", message="%(prog)s %(version)s")
@click.option("--debug", is_flag=True, envvar=DEBUG_ENV_VAR, help="Enable debug output")
def main(debug: bool) -> None:
    """Epoxy VPN manager."""
    _common.DEBUG = debug


main.add_command(up.up)
main.add_command(connect.connect)
main.add_command(down.down)
main.add_command(rm.rm)
main.add_command(logs.logs)
main.add_command(status.status)
main.add_command(bench.bench)
main.add_command(ls.ls)
main.add_command(dns.dns)
main.add_command(update.update)
main.add_command(install.install)
