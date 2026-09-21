"""Tests for `--all`: one invocation acting on every known instance."""

import json
from subprocess import CompletedProcess

import click
import pytest
from click.testing import CliRunner

from epoxy import cli, config, discovery
from epoxy.commands import _common
from epoxy.instance import current_instance

ALL_COMMANDS = ["status", "down", "rm", "logs", "dns", "update"]
SINGLE_ONLY = ["up", "connect", "bench", "ls"]


def invoke(args, **kwargs):
    return CliRunner().invoke(cli.main, args, **kwargs)


@pytest.fixture()
def two_instances(monkeypatch):
    """Two known instances (a, b) resolvable without docker."""
    monkeypatch.setattr(discovery, "_known_names", lambda: {"b", "a"})
    monkeypatch.setattr("epoxy.docker.container_control_port", lambda name=None: None)
    return ("a", "b")


@pytest.fixture()
def no_instances(monkeypatch):
    monkeypatch.setattr(discovery, "_known_names", lambda: set())
    return ()


def test_all_option_present_where_supported():
    for name in ALL_COMMANDS:
        params = {p.name for p in cli.main.commands[name].params}
        assert "all_instances" in params, name
    for name in SINGLE_ONLY:
        params = {p.name for p in cli.main.commands[name].params}
        assert "all_instances" not in params, name


def test_instance_and_all_conflict():
    cases = [
        ["status", "--no-speedtest"],
        ["down"],
        ["rm"],
        ["logs"],
        ["dns"],
        ["update"],
    ]
    for args in cases:
        result = invoke([*args, "--instance", "a", "--all"])
        assert result.exit_code == 2, args
        assert "--all" in result.output and "--instance" in result.output


def test_all_ignores_env_and_never_prompts(monkeypatch, two_instances):
    """--all ignores EPOXY_INSTANCE and never touches the picker."""
    monkeypatch.setenv("EPOXY_INSTANCE", "bogus")
    monkeypatch.setattr(_common, "_stdin_is_tty", lambda: pytest.fail("must not prompt"))
    monkeypatch.setattr(_common, "_choose_instance_name", lambda: pytest.fail("must not choose"))
    projects: list[str] = []
    monkeypatch.setattr("epoxy.control.set_tunnel_status", lambda *a, **kw: None)

    def fake_compose(*args: str, env_overrides=None, timeout=None):
        projects.append(current_instance().project)
        return CompletedProcess((), 0)

    monkeypatch.setattr("epoxy.docker.compose", fake_compose)
    result = invoke(["down", "--all"], catch_exceptions=False)
    assert result.exit_code == 0
    assert projects == ["epoxy-a", "epoxy-b"]


def test_down_all_stops_each_and_reports_prefixed(monkeypatch, two_instances):
    stopped: list[str] = []
    monkeypatch.setattr(
        "epoxy.control.set_tunnel_status", lambda *a, **kw: stopped.append(current_instance().name)
    )
    monkeypatch.setattr("epoxy.docker.compose", lambda *a, **kw: CompletedProcess((), 0))
    result = invoke(["down", "--all"], catch_exceptions=False)
    assert result.exit_code == 0
    assert stopped == ["a", "b"]
    assert "a: VPN stopped." in result.output
    assert "b: VPN stopped." in result.output


def test_down_all_continues_past_failure(monkeypatch, two_instances):
    monkeypatch.setattr("epoxy.control.set_tunnel_status", lambda *a, **kw: None)

    def fake_compose(*args: str, env_overrides=None, timeout=None):
        if current_instance().name == "a":
            raise SystemExit("Error: daemon exploded")
        return CompletedProcess((), 0)

    monkeypatch.setattr("epoxy.docker.compose", fake_compose)
    result = invoke(["down", "--all"])
    assert result.exit_code == 1
    assert "daemon exploded" in result.output
    assert "b: VPN stopped." in result.output


def test_down_all_empty_reports_no_instances(no_instances):
    result = invoke(["down", "--all"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "(no instances)" in result.output


def test_logs_all_prints_header_per_instance(monkeypatch, two_instances):
    seen: list[tuple[str, tuple[str, ...]]] = []

    def fake_compose(*args: str, **kwargs: object) -> CompletedProcess[str]:
        seen.append((current_instance().name, args))
        return CompletedProcess((), 0)

    monkeypatch.setattr("epoxy.docker.compose", fake_compose)
    result = invoke(["logs", "--all"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "== a ==" in result.output
    assert "== b ==" in result.output
    assert [name for name, _ in seen] == ["a", "b"]


def test_logs_follow_with_all_is_usage_error():
    result = invoke(["logs", "--all", "--follow"])
    assert result.exit_code == 2
    assert "--follow" in result.output


def test_status_all_json_envelope(monkeypatch, two_instances):
    def fake_doc():
        name = current_instance().name
        return {
            "instance": name,
            "container_name": name,
            "image": "img",
            "state": "running",
            "selection": None,
            "drift": False,
            "control_server": {"port": 8000, "enabled": True},
            "exit_ip": None,
            "leak": False,
            "verified": False,
            "last_error": None,
        }

    monkeypatch.setattr("epoxy.commands.status._status_doc", fake_doc)
    result = invoke(["status", "--all", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert [i["instance"] for i in doc["instances"]] == ["a", "b"]


def test_status_all_json_empty_is_empty_envelope(no_instances):
    result = invoke(["status", "--all", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    assert json.loads(result.output) == {"instances": []}


def test_status_all_json_leak_in_one_fails(monkeypatch, two_instances):
    def fake_doc():
        name = current_instance().name
        return {
            "instance": name,
            "state": "running",
            "selection": None,
            "drift": False,
            "control_server": {"port": 8000, "enabled": True},
            "exit_ip": {"ip": "1.2.3.4", "country": None},
            "leak": name == "b",
            "verified": name != "b",
            "last_error": None,
        }

    monkeypatch.setattr("epoxy.commands.status._status_doc", fake_doc)
    result = invoke(["status", "--all", "--json"])
    assert result.exit_code == 1
    assert len(json.loads(result.output)["instances"]) == 2


def test_status_all_human_headers_and_continues(monkeypatch, two_instances):
    def fake_human(size: int, no_speedtest: bool) -> None:
        if current_instance().name == "a":
            raise click.ClickException("boom")
        click.echo("fine")

    monkeypatch.setattr("epoxy.commands.status._print_human_status", fake_human)
    result = invoke(["status", "--all", "--no-speedtest"])
    assert result.exit_code == 1
    assert "== a ==" in result.output
    assert "== b ==" in result.output
    assert "fine" in result.output


def test_status_all_human_empty(no_instances):
    result = invoke(["status", "--all", "--no-speedtest"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "(no instances)" in result.output


def test_dns_all_shows_each(monkeypatch, two_instances):
    monkeypatch.setattr("epoxy.control.get_dns_status", lambda: "running")
    result = invoke(["dns", "--all"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "a: DNS: running" in result.output
    assert "b: DNS: running" in result.output


def test_dns_all_sets_each_and_continues(monkeypatch, two_instances):
    from epoxy.control import ControlError

    targets: list[str] = []

    def fake_set(target: str) -> None:
        if current_instance().name == "a":
            raise ControlError(None, "down")
        targets.append(target)

    monkeypatch.setattr("epoxy.control.set_dns_status", fake_set)
    result = invoke(["dns", "--all", "on"])
    assert result.exit_code == 1
    assert targets == ["running"]
    assert "b: DNS running." in result.output


def test_update_all_triggers_each(monkeypatch, two_instances):
    triggered: list[str] = []
    monkeypatch.setattr(
        "epoxy.control.trigger_updater", lambda: triggered.append(current_instance().name)
    )
    result = invoke(["update", "--all"], catch_exceptions=False)
    assert result.exit_code == 0
    assert triggered == ["a", "b"]
    assert "a: Server list update triggered." in result.output
    assert "b: Server list update triggered." in result.output


def _seed(name: str) -> None:
    config.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    (config.INSTANCES_DIR / f"{name}.json").write_text(
        json.dumps({"instance": name, "control_port": 8123, "env_file": None})
    )
    compose_dir = config.INSTANCES_DIR / name
    compose_dir.mkdir(parents=True, exist_ok=True)
    (compose_dir / "compose.yml").write_text(f"container_name: {name}\n")


def test_rm_all_skips_shared_without_force(monkeypatch, two_instances):
    _seed("a")
    _seed("b")
    monkeypatch.setattr("epoxy.control.set_tunnel_status", lambda *a, **kw: None)
    monkeypatch.setattr("epoxy.docker.compose", lambda *args, **kw: CompletedProcess((), 0))
    monkeypatch.setattr(discovery, "consumers_of", lambda name: ["web"] if name == "a" else [])

    result = invoke(["rm", "--all"])
    assert result.exit_code == 1
    assert "shared by" in result.output
    assert (config.INSTANCES_DIR / "a.json").exists()  # kept
    assert not (config.INSTANCES_DIR / "b.json").exists()  # removed
    assert "Instance 'b' removed." in result.output


def test_rm_all_force_removes_everything(monkeypatch, two_instances):
    _seed("a")
    _seed("b")
    monkeypatch.setattr("epoxy.control.set_tunnel_status", lambda *a, **kw: None)
    monkeypatch.setattr("epoxy.docker.compose", lambda *args, **kw: CompletedProcess((), 0))
    monkeypatch.setattr(discovery, "consumers_of", lambda name: ["web"] if name == "a" else [])

    result = invoke(["rm", "--all", "--force"], catch_exceptions=False)
    assert result.exit_code == 0
    assert not (config.INSTANCES_DIR / "a.json").exists()
    assert not (config.INSTANCES_DIR / "b.json").exists()
