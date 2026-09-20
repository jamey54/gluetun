"""Tests for `vpn rm`: container removal plus registry-state deletion."""

import json
from pathlib import Path
from subprocess import CompletedProcess

from click.testing import CliRunner

from vpn import cli, config


def invoke(args):
    return CliRunner().invoke(cli.main, args, catch_exceptions=False)


def _seed_state(name: str = "gluetun") -> None:
    """Registry record, generated compose file, and lockfile for an instance."""
    config.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    (config.INSTANCES_DIR / f"{name}.json").write_text(
        json.dumps({"instance": name, "control_port": 8123, "env_file": None})
    )
    compose_dir = config.INSTANCES_DIR / name
    compose_dir.mkdir(parents=True, exist_ok=True)
    (compose_dir / "compose.yml").write_text(f"container_name: {name}\n")
    config.LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    (config.LOCKS_DIR / f"{name}.lock").write_text("lock")


def _state_paths(name: str = "gluetun") -> list[Path]:
    return [
        config.INSTANCES_DIR / f"{name}.json",
        config.INSTANCES_DIR / name / "compose.yml",
        config.LOCKS_DIR / f"{name}.lock",
    ]


def _stub_docker_ok(monkeypatch, compose_calls):
    monkeypatch.setattr("vpn.control.set_vpn_status", lambda *a, **kw: None)

    def fake_compose(*args: str, env_overrides=None, timeout=None):
        compose_calls.append(args)
        return CompletedProcess((), 0)

    monkeypatch.setattr("vpn.cli.compose", fake_compose)
    monkeypatch.setattr("vpn.cli.remove_container", lambda name: None)


def test_rm_removes_container_and_deletes_state(monkeypatch):
    from vpn import discovery

    _seed_state()
    compose_calls: list[tuple[str, ...]] = []
    _stub_docker_ok(monkeypatch, compose_calls)
    monkeypatch.setattr(discovery, "_known_names", lambda: {"gluetun"})
    monkeypatch.setattr(discovery, "consumers_of", lambda name: [])

    result = invoke(["rm", "--instance", "gluetun"])
    assert result.exit_code == 0
    assert "Instance 'gluetun' removed." in result.output
    assert compose_calls == [("down",)]
    assert [p.exists() for p in _state_paths()] == [False, False, False]


def test_rm_refuses_consumers_without_force(monkeypatch):
    from vpn import discovery

    _seed_state()
    compose_calls: list[tuple[str, ...]] = []
    _stub_docker_ok(monkeypatch, compose_calls)
    monkeypatch.setattr(discovery, "_known_names", lambda: {"gluetun"})
    monkeypatch.setattr(discovery, "consumers_of", lambda name: ["web-app"])

    result = CliRunner().invoke(cli.main, ["rm", "--instance", "gluetun"])
    assert result.exit_code == 1
    assert "web-app" in result.output
    assert "--force" in result.output
    assert compose_calls == []
    assert (config.INSTANCES_DIR / "gluetun.json").exists()


def test_rm_force_removes_with_consumers(monkeypatch):
    from vpn import discovery

    _seed_state()
    compose_calls: list[tuple[str, ...]] = []
    _stub_docker_ok(monkeypatch, compose_calls)
    monkeypatch.setattr(discovery, "_known_names", lambda: {"gluetun"})
    monkeypatch.setattr(discovery, "consumers_of", lambda name: ["web-app"])

    result = invoke(["rm", "--instance", "gluetun", "--force"])
    assert result.exit_code == 0
    assert compose_calls == [("down",)]
    assert [p.exists() for p in _state_paths()] == [False, False, False]


def test_rm_unknown_instance_is_friendly(monkeypatch):
    from vpn import discovery

    compose_calls: list[tuple[str, ...]] = []
    _stub_docker_ok(monkeypatch, compose_calls)
    monkeypatch.setattr(discovery, "_known_names", lambda: {"other"})

    result = CliRunner().invoke(cli.main, ["rm", "--instance", "gluetun"])
    assert result.exit_code == 1
    assert "Unknown instance 'gluetun'." in result.output
    assert compose_calls == []


def test_rm_without_compose_file_falls_back_to_docker_rm(monkeypatch):
    from vpn import discovery

    config.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    (config.INSTANCES_DIR / "gluetun.json").write_text(
        json.dumps({"instance": "gluetun", "control_port": 8123, "env_file": None})
    )
    removed: list[str] = []
    monkeypatch.setattr("vpn.control.set_vpn_status", lambda *a, **kw: None)

    def no_compose(*args: str, env_overrides=None, timeout=None):
        raise AssertionError("compose file is absent, compose must not run")

    monkeypatch.setattr("vpn.cli.compose", no_compose)
    monkeypatch.setattr("vpn.cli.remove_container", lambda name: removed.append(name))
    monkeypatch.setattr(discovery, "_known_names", lambda: {"gluetun"})
    monkeypatch.setattr(discovery, "consumers_of", lambda name: [])

    result = invoke(["rm", "--instance", "gluetun"])
    assert result.exit_code == 0
    assert removed == ["gluetun"]
    assert not (config.INSTANCES_DIR / "gluetun.json").exists()


def test_rm_has_instance_and_force_options():
    params = {p.name for p in cli.main.commands["rm"].params}
    assert {"instance", "force"} <= params
