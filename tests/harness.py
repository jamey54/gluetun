"""Single entry point for driving the CLI in tests.

Every instance-scoped command requires `--instance`, and the product deliberately
no longer reads a name from the environment. Injecting the flag here — rather
than at ~130 call sites — keeps "tests target the `epoxy` instance" true in one
place and leaves each test's own arguments about what it is actually testing.
"""

from typing import Any

from click.testing import CliRunner, Result

from epoxy import cli

#: The instance tests target unless they name another one explicitly.
TEST_INSTANCE = "epoxy"

#: Commands whose ``--instance`` filters a listing rather than naming a target to
#: act on. Injecting the test instance into one of these would hide every record
#: except that one — the opposite of what a listing test wants. ``ls`` is the only
#: one; test_cli_contract asserts that every other --instance command genuinely
#: errors without one.
FILTER_ONLY = frozenset({"ls"})


def run_cli(args: list[str], *, catch_exceptions: bool = True, **kwargs: Any) -> Result:
    """Invoke ``epoxy`` with an instance named, unless the test named one.

    The flag is inserted directly after the subcommand, because options belong to
    the subcommand rather than the group. Commands that declare no ``--instance``
    (``install``) and group-level options (``--version``) pass through untouched,
    so this cannot paper over a genuinely missing flag. ``--all`` fan-out is
    skipped too, since naming one instance is exactly what it must not do.
    """
    argv = list(args)
    if argv and not argv[0].startswith("-") and "--all" not in argv:
        command = cli.main.commands.get(argv[0])
        takes_instance = (
            command is not None
            and argv[0] not in FILTER_ONLY
            and any(param.name == "instance" for param in command.params)
        )
        if takes_instance and "--instance" not in argv:
            argv = [argv[0], "--instance", TEST_INSTANCE, *argv[1:]]
    return CliRunner().invoke(cli.main, argv, catch_exceptions=catch_exceptions, **kwargs)


def run_bare_cli(args: list[str], *, catch_exceptions: bool = True, **kwargs: Any) -> Result:
    """Invoke ``epoxy`` exactly as given, without injecting ``--instance``.

    For the tests that pin the missing-instance behavior itself (usage error,
    interactive picking): ``run_cli`` would name the test instance and hide
    what they assert.
    """
    return CliRunner().invoke(cli.main, list(args), catch_exceptions=catch_exceptions, **kwargs)
