"""Epoxy CLI entry point: command group, shared flags, and command wiring."""

import sys

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
    rotate,
    servers,
    status,
    tui,
    up,
    update,
)
from epoxy.config import DEBUG_ENV_VAR
from epoxy.version import __version__


def _interactive() -> bool:
    """True when both ends are terminals, i.e. a TUI can actually render."""
    return sys.stdin.isatty() and sys.stdout.isatty()


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="epoxy", message="%(prog)s %(version)s")
@click.option("--debug", is_flag=True, envvar=DEBUG_ENV_VAR, help="Enable debug output")
@click.pass_context
def main(ctx: click.Context, debug: bool) -> None:
    """Epoxy VPN manager.

    Run without a subcommand on a terminal to open the interactive UI; use a
    subcommand for automation and machine-readable output.
    """
    _common.DEBUG = debug
    if ctx.invoked_subcommand is None:
        if _interactive():
            from epoxy.tui.app import EpoxyApp

            EpoxyApp().run()
        else:
            click.echo(ctx.get_help())


main.add_command(up.up)
main.add_command(connect.connect)
main.add_command(rotate.rotate)
main.add_command(down.down)
main.add_command(rm.rm)
main.add_command(logs.logs)
main.add_command(status.status)
main.add_command(bench.bench)
main.add_command(ls.ls)
main.add_command(servers.servers_cmd)
main.add_command(dns.dns)
main.add_command(update.update)
main.add_command(install.install)
main.add_command(tui.tui)
