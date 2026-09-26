"""Guards for the public CLI surface dockerstrator depends on.

The consumer contract (README, "Consumer API") is additions-only: flag names are
never removed or renamed, and consumers detect a too-old epoxy by passing a flag
that does not exist ("if ``epoxy ls --json`` exits non-zero or reports an unknown
flag, treat epoxy as pre-0.3").

Click derives flag spellings from the Python parameter name, so renaming a
parameter silently changes the flag (``no_speedtest`` -> ``skip_speedtest``
emits ``--no_speedtest``) and a consumer would quietly fall back to shared-only
mode. These tests pin the flag spellings so that rename breaks the build instead.
"""

import pytest

from epoxy import cli

#: Every long flag and short alias each command accepts today. Adding one is
#: allowed (the contract is additions-only); removing or renaming one is not, so
#: the assertion is membership, not equality.
EXPECTED_FLAGS = {
    "bench": {
        "--concurrency",
        "-c",
        "--connect",
        "--country",
        "--instance",
        "--max-candidates",
        "-n",
        "--protocol",
        "--provider",
        "--scan-size",
        "-s",
        "--size",
        "--top",
    },
    "connect": {
        "--city",
        "--country",
        "--instance",
        "--list",
        "--no-speedtest",
        "--protocol",
        "--provider",
    },
    "dns": {"--all", "--instance"},
    "down": {"--all", "--instance"},
    "install": {"--force", "--print", "--rc-file", "--shell"},
    "logs": {"--all", "--follow", "-f", "--instance", "-n", "--tail"},
    "ls": {"--instance", "--json"},
    "rm": {"--all", "--force", "-f", "--instance"},
    "status": {"--all", "--instance", "--json", "--no-speedtest", "-s", "--size"},
    "up": {
        "--city",
        "--country",
        "--ctl-port",
        "--env-file",
        "--instance",
        "--no-speedtest",
        "--protocol",
        "--provider",
        "--pull",
        "--recreate",
    },
    "update": {"--all", "--instance"},
}

#: Options whose flag spelling differs from a naive guess, so a rename here is
#: especially easy to miss in review.
PINNED_SPELLINGS = {
    ("connect", "--list"),
    ("ls", "--json"),
    ("status", "--json"),
    ("logs", "--follow"),
    ("up", "--no-speedtest"),
    ("connect", "--no-speedtest"),
    ("status", "--no-speedtest"),
    ("logs", "--tail"),
    ("install", "--print"),
    ("install", "--rc-file"),
    ("up", "--ctl-port"),
    ("up", "--env-file"),
    ("bench", "--max-candidates"),
    ("bench", "--scan-size"),
}


def flags_of(name: str) -> set[str]:
    """Every option spelling (long and short) the command accepts."""
    return {opt for param in cli.main.commands[name].params for opt in param.opts}


def test_every_command_is_pinned():
    """A new command must be added here deliberately, not appear unannounced."""
    assert set(EXPECTED_FLAGS) == set(cli.main.commands)


@pytest.mark.parametrize("name", sorted(EXPECTED_FLAGS))
def test_command_keeps_its_documented_flags(name):
    missing = EXPECTED_FLAGS[name] - flags_of(name)
    assert not missing, f"{name} lost flag(s) {sorted(missing)}"


@pytest.mark.parametrize(("name", "flag"), sorted(PINNED_SPELLINGS))
def test_pinned_spellings_exist(name, flag):
    assert flag in flags_of(name)


def test_instance_is_the_first_option_of_every_command():
    """--instance is documented as first; click lists options in reverse order."""
    for name in ("status", "down", "rm", "logs", "dns", "update", "up", "connect", "bench", "ls"):
        assert cli.main.commands[name].params[0].name == "instance", name


def test_commands_that_fan_out_accept_all_and_others_do_not():
    """`up`/`connect`/`bench` deliberately take no --all (README, "Acting on all")."""
    for name in ("status", "down", "rm", "logs", "dns", "update"):
        assert "--all" in flags_of(name), name
    for name in ("up", "connect", "bench", "ls", "install"):
        assert "--all" not in flags_of(name), name


def test_version_option_is_preserved():
    """`epoxy --version` is part of the capability probe."""
    assert "--version" in {opt for param in cli.main.params for opt in param.opts}
