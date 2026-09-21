"""Wire shell completion into your rc file."""

from pathlib import Path

import click

from epoxy.install import completion_block, default_rc, detect_shell, path_hint, upsert_block


@click.command()
@click.option(
    "--shell",
    type=click.Choice(["auto", "bash", "zsh", "fish"]),
    default="auto",
    show_default=True,
    help="Shell to wire completion for (default: detect from $SHELL)",
)
@click.option(
    "--rc-file",
    type=click.Path(dir_okay=False, path_type=str),
    default=None,
    help="Rc file to write (default: ~/.bashrc, ~/.zshrc, or the fish config)",
)
@click.option(
    "--print",
    "print_only",
    is_flag=True,
    help="Print the snippet instead of writing it",
)
@click.option(
    "--force",
    is_flag=True,
    help="Replace an existing completion block with different content",
)
def install(shell: str, rc_file: str | None, print_only: bool, force: bool) -> None:
    """Wire shell completion into your rc file (preview with --print)."""
    resolved = detect_shell(shell)
    block = completion_block(resolved)
    hint = path_hint()
    if print_only:
        if hint:
            click.echo(hint)
        click.echo(block, nl=False)
        return
    rc = Path(rc_file) if rc_file else default_rc(resolved)
    result = upsert_block(rc, block, force)
    if hint:
        click.echo(hint)
    if result == "unchanged":
        click.echo(f"Completion already installed for {resolved} in {rc}.")
    else:
        click.echo(f"Completion {result} for {resolved} in {rc} — reload your shell.")
