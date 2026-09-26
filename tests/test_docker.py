"""Tests for docker helpers: container env reading and compose invocation."""

import subprocess
from typing import Any

import pytest

from epoxy import config, docker


@pytest.fixture()
def inspect_env(monkeypatch):
    def set_env(lines):
        monkeypatch.setattr(docker, "inspect_container", lambda fmt: "\n".join(lines))

    return set_env


def test_container_env_parses_all_vars(inspect_env):
    inspect_env(
        [
            "PATH=/usr/bin",
            "VPN_SERVICE_PROVIDER=surfshark",
            "VPN_TYPE=openvpn",
            "SERVER_COUNTRIES=Germany",
            "SERVER_CITIES=",
        ]
    )
    assert docker.container_env() == {
        "PATH": "/usr/bin",
        "VPN_SERVICE_PROVIDER": "surfshark",
        "VPN_TYPE": "openvpn",
        "SERVER_COUNTRIES": "Germany",
        "SERVER_CITIES": "",
    }


def test_container_env_missing_container(monkeypatch):
    monkeypatch.setattr(docker, "inspect_container", lambda fmt: None)
    assert docker.container_env() == {}


# ---------------------------------------------------------------------------
# compose invocation
# ---------------------------------------------------------------------------


def test_compose_merges_env_without_tempfile(monkeypatch):
    from subprocess import CompletedProcess

    seen_args: tuple[str, ...] = ()
    seen_env: dict[str, str] | None = None

    def fake_run(*args, capture=False, check=True, env=None, timeout=None):
        nonlocal seen_args, seen_env
        seen_args, seen_env = args, env
        return CompletedProcess(args, 0)

    monkeypatch.setattr(docker, "run", fake_run)
    docker.compose("up", "-d", env_overrides={"WIREGUARD_PRIVATE_KEY": "secret"})

    assert seen_args[:4] == (
        "docker",
        "compose",
        "-f",
        str(config.INSTANCES_DIR / "epoxy" / "compose.yml"),
    )
    assert seen_args[4] == "-p"
    assert seen_args[5] == "epoxy-epoxy"
    assert seen_env is not None
    assert seen_env["WIREGUARD_PRIVATE_KEY"] == "secret"
    assert seen_env["PATH"]  # process env preserved


# ---------------------------------------------------------------------------
# disposable bench containers
# ---------------------------------------------------------------------------


def test_launch_container_builds_docker_run_args(monkeypatch):
    from subprocess import CompletedProcess

    seen: dict[str, Any] = {}

    def fake_run(*args, capture=False, check=False, timeout=None):
        seen["args"], seen["capture"], seen["check"] = args, capture, check
        return CompletedProcess(args, 0)

    monkeypatch.setattr(docker, "run", fake_run)
    ok = docker.launch_container(
        "epoxy-bench-123-0",
        {"VPN_SERVICE_PROVIDER": "surfshark", "SERVER_COUNTRIES": "Germany"},
    )

    assert ok is True
    args = seen["args"]
    assert args[:8] == (
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        "epoxy-bench-123-0",
        "--cap-add",
        "NET_ADMIN",
    )
    assert args[8:11] == ("--device", "/dev/net/tun:/dev/net/tun", "-e")
    assert "VPN_SERVICE_PROVIDER=surfshark" in args
    assert "SERVER_COUNTRIES=Germany" in args
    assert args[-1] == config.image_ref()
    assert seen["capture"] is True and seen["check"] is False


def test_launch_container_uses_pinned_image_by_default(monkeypatch):
    monkeypatch.delenv("EPOXY_IMAGE", raising=False)
    assert config.image_ref() == config.DEFAULT_IMAGE
    assert config.DEFAULT_IMAGE == "qmcgaw/gluetun:v3.41.3"


def test_launch_container_honors_image_override(monkeypatch):
    monkeypatch.setenv("EPOXY_IMAGE", "qmcgaw/gluetun:v3.40.0")
    assert config.image_ref() == "qmcgaw/gluetun:v3.40.0"


def test_launch_container_failure_reported(monkeypatch):
    from subprocess import CompletedProcess

    monkeypatch.setattr(docker, "run", lambda *args, **kw: CompletedProcess(args, 1))
    assert docker.launch_container("x", {}) is False


def test_remove_container_best_effort(monkeypatch):
    from subprocess import CompletedProcess

    seen = {}
    monkeypatch.setattr(
        docker,
        "run",
        lambda *args, **kw: seen.update({"args": args, "kw": kw}) or CompletedProcess(args, 1),
    )
    docker.remove_container("whatever")  # must not raise on failure
    assert seen["args"] == ("docker", "rm", "-f", "whatever")
    assert seen["kw"]["check"] is False


def test_launch_container_survives_missing_docker(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kw: (_ for _ in ()).throw(
            OSError("[Errno 2] No such file or directory: 'docker'")
        ),
    )
    assert docker.launch_container("x", {}) is False
    docker.remove_container("x")  # must not raise


def test_run_timeout_surfaces_as_exit_code_124():
    result = docker.run("sleep", "60", capture=True, check=False, timeout=0.3)
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_run_timeout_with_check_raises_system_exit():
    with pytest.raises(SystemExit):
        docker.run("sleep", "60", timeout=0.3)


def test_run_missing_command_check_false_returns_127(capsys):
    result = docker.run("no-such-binary-xyz", capture=True, check=False)
    assert result.returncode == 127
    assert "no-such-binary-xyz" in result.stderr
    assert capsys.readouterr().err == ""  # non-fatal path stays quiet


def test_run_missing_command_check_true_exits_127(capsys):
    with pytest.raises(SystemExit) as ei:
        docker.run("no-such-binary-xyz")
    assert ei.value.code == 127
    assert "no-such-binary-xyz" in capsys.readouterr().err


def test_inspect_container_bounds_stalled_daemon(monkeypatch):
    from subprocess import CompletedProcess

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        docker,
        "run",
        lambda *args, **kw: seen.update(kw) or CompletedProcess(args, 0),
    )
    docker.container_status("epoxy")
    assert seen["timeout"] == config.CONTAINER_OP_TIMEOUT_S


def test_inspect_container_timeout_reads_as_absent(monkeypatch):
    from subprocess import CompletedProcess

    monkeypatch.setattr(
        docker, "run", lambda *args, **kw: CompletedProcess(args, 124, stdout="", stderr="timeout")
    )
    assert docker.container_status("epoxy") is None


def test_inspect_container_reads_missing_docker_as_absent(monkeypatch):
    """A missing docker binary is absence, not a crash, for read-only probes.

    run(check=False) turns the OSError out of subprocess into returncode 127
    rather than raising, so inspect_container's non-zero-returncode check already
    covers it and needs no handler of its own.
    """

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("no such file or directory: 'docker'")

    monkeypatch.setattr(subprocess, "run", boom)
    assert docker.inspect_container("{{.State.Status}}", name="epoxy") is None
    assert docker.container_status("epoxy") is None
    # check=True keeps treating the same OSError as a hard, reported failure.
    with pytest.raises(SystemExit) as ei:
        docker.run("docker", "inspect", "epoxy")
    assert ei.value.code == 127


def test_container_control_port_reads_the_container_port(monkeypatch):
    """The published-port lookup keys on the in-container port, not the host one."""
    published = f'{{"{config.CONTAINER_CTL_PORT}/tcp": [{{"HostPort": "8123"}}]}}'
    monkeypatch.setattr(
        docker, "inspect_container", lambda fmt, name=None: published
    )
    assert docker.container_control_port("epoxy") == 8123
