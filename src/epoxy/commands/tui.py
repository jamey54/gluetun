"""Open the interactive terminal UI."""

import sys

import click


@click.command()
def tui() -> None:
    """Open the interactive terminal UI."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise click.UsageError(
            "The interactive UI needs a terminal. Run it from a shell, or use a "
            "CLI subcommand for automation."
        )
    from epoxy.tui.app import EpoxyApp

    EpoxyApp().run()
