"""Tests for `epoxy install`: rc wiring, idempotency, preview, and PATH hint."""

import shutil

from click.testing import CliRunner

from epoxy import cli
from epoxy.install import END_MARKER, START_MARKER, completion_block


def invoke(args: list[str]):
    return CliRunner().invoke(cli.main, args)


def test_install_writes_marker_block(tmp_path, monkeypatch):
    rc = tmp_path / ".zshrc"
    monkeypatch.setenv("SHELL", "/bin/zsh")
    result = invoke(["install", "--rc-file", str(rc)])
    assert result.exit_code == 0, result.output
    body = rc.read_text()
    assert START_MARKER in body and END_MARKER in body
    assert 'eval "$(_EPOXY_COMPLETE=zsh_source epoxy)"' in body
    assert f"Completion written for zsh in {rc}" in result.output


def test_install_is_idempotent(tmp_path, monkeypatch):
    rc = tmp_path / ".bashrc"
    rc.write_text("# my stuff\nexport FOO=1\n")
    monkeypatch.setenv("SHELL", "/bin/bash")
    first = invoke(["install", "--rc-file", str(rc)])
    assert first.exit_code == 0, first.output
    before = rc.read_text()
    assert before.startswith("# my stuff\nexport FOO=1\n")
    assert before.count(START_MARKER) == 1
    second = invoke(["install", "--rc-file", str(rc)])
    assert second.exit_code == 0, second.output
    assert rc.read_text() == before
    assert "already installed" in second.output


def test_install_print_writes_nothing(tmp_path, monkeypatch):
    rc = tmp_path / ".bashrc"
    monkeypatch.setenv("SHELL", "/bin/bash")
    result = invoke(["install", "--rc-file", str(rc), "--print"])
    assert result.exit_code == 0, result.output
    assert 'eval "$(_EPOXY_COMPLETE=bash_source epoxy)"' in result.output
    assert not rc.exists()


def test_install_fish_snippet(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/fish")
    block = completion_block("fish")
    assert block == (
        f"{START_MARKER}\neval (env _EPOXY_COMPLETE=fish_source epoxy)\n{END_MARKER}\n"
    )


def test_install_conflicting_block_needs_force(tmp_path, monkeypatch):
    rc = tmp_path / ".zshrc"
    rc.write_text(f"{START_MARKER}\neval old\n{END_MARKER}\n")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    result = invoke(["install", "--rc-file", str(rc)])
    assert result.exit_code == 2
    assert "--force" in result.output
    assert "eval old" in rc.read_text()
    forced = invoke(["install", "--rc-file", str(rc), "--force"])
    assert forced.exit_code == 0, forced.output
    assert "eval old" not in rc.read_text()
    assert "Completion updated" in forced.output


def test_install_unknown_shell_is_usage_error(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/pwsh")
    result = invoke(["install", "--rc-file", "/nonexistent-rc"])
    assert result.exit_code == 2
    assert "--shell" in result.output


def test_install_warns_when_epoxy_not_on_path(tmp_path, monkeypatch):
    rc = tmp_path / ".bashrc"
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda _: None)
    result = invoke(["install", "--rc-file", str(rc)])
    assert result.exit_code == 0, result.output
    assert "not on PATH" in result.output
    assert START_MARKER in rc.read_text()


def test_install_default_rc_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    result = invoke(["install"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".bashrc").exists()
