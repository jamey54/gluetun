"""CLI tests for the per-instance surface: naming, ports, env files, exit codes."""

import json
from pathlib import Path
from subprocess import CompletedProcess

import click
import pytest

from epoxy import apply, cli, config, discovery, docker, ipinfo, picker
from epoxy.apply import Selection
from epoxy.commands import _common
from epoxy.instance import current_instance
from epoxy.version import __version__
from tests.harness import run_bare_cli, run_cli


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("SURFSHARK_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("PROTONVPN_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("PROTONVPN_WIREGUARD_ADDRESSES", "10.2.0.2/32")
    monkeypatch.setenv("HTTP_CONTROL_SERVER_API_KEY", "test-key")
    monkeypatch.setattr("epoxy.ipinfo.print_ip_status", lambda **kwargs: True)
    monkeypatch.setattr("epoxy.speedtest.measure", lambda size=25, **kwargs: None)
    monkeypatch.setattr("epoxy.ipinfo.current_exit_ip", lambda: None)


@pytest.fixture()
def compose_calls(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_compose(
        *args: str,
        env_overrides: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> CompletedProcess[str]:
        calls.append(args)
        return CompletedProcess((), 0)

    monkeypatch.setattr("epoxy.docker.compose", fake_compose)
    return calls


@pytest.fixture()
def cold(monkeypatch):
    monkeypatch.setattr(docker, "container_running", lambda name=None: False)


def read_registry(name: str) -> dict[str, object]:
    return json.loads((config.INSTANCES_DIR / f"{name}.json").read_text())


def test_up_non_default_instance_writes_compose_and_registry(compose_calls, cold, monkeypatch):
    monkeypatch.setattr("epoxy.commands.up.allocate_free_port", lambda: 8123)
    result = run_cli(["up", "--instance", "plan-a", "--provider", "surfshark"])
    assert result.exit_code == 0

    generated = config.INSTANCES_DIR / "plan-a" / "compose.yml"
    body = generated.read_text()
    assert "container_name: plan-a" in body
    assert "127.0.0.1:8123:8000/tcp" in body
    assert read_registry("plan-a") == {"instance": "plan-a", "control_port": 8123, "env_file": None}
    assert compose_calls == [("up", "-d")]


def test_up_non_default_instance_reuses_registered_port(compose_calls, cold, monkeypatch):
    config.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    (config.INSTANCES_DIR / "plan-a.json").write_text(
        json.dumps({"instance": "plan-a", "control_port": 8123, "env_file": None})
    )
    monkeypatch.setattr(
        "epoxy.commands.up.allocate_free_port",
        lambda: pytest.fail("must not allocate"),
    )
    result = run_cli(["up", "--instance", "plan-a", "--provider", "surfshark"])
    assert result.exit_code == 0
    body = (config.INSTANCES_DIR / "plan-a" / "compose.yml").read_text()
    assert "127.0.0.1:8123:8000/tcp" in body
    assert read_registry("plan-a")["control_port"] == 8123


def test_up_renders_compose_inside_instance_context(compose_calls, cold, monkeypatch):
    """`up` enters its instance context before writing the compose file.

    Rendering resolves image_ref() through the active instance's env. Outside a
    context that falls back to default_instance() — a usage error in production,
    papered over in tests by the conftest fallback. With the fallback disabled
    this reproduces production, so it goes red if ensure_compose_file ever moves
    back above the context.
    """
    monkeypatch.setattr(
        "epoxy.instance.default_instance",
        lambda: pytest.fail("must not fall back to default_instance"),
    )
    monkeypatch.setattr("epoxy.commands.up.allocate_free_port", lambda: 8123)
    result = run_cli(["up", "--instance", "plan-a", "--provider", "surfshark"])
    assert result.exit_code == 0
    body = (config.INSTANCES_DIR / "plan-a" / "compose.yml").read_text()
    assert "container_name: plan-a" in body
    assert read_registry("plan-a")["control_port"] == 8123


def test_up_ctl_port_wins_over_allocation(compose_calls, cold, monkeypatch):
    monkeypatch.setattr(
        "epoxy.commands.up.allocate_free_port",
        lambda: pytest.fail("must not allocate"),
    )
    result = run_cli(
        ["up", "--instance", "plan-b", "--ctl-port", "8300", "--provider", "surfshark"]
    )
    assert result.exit_code == 0
    assert read_registry("plan-b")["control_port"] == 8300
    assert (
        "127.0.0.1:8300:8000/tcp" in (config.INSTANCES_DIR / "plan-b" / "compose.yml").read_text()
    )


def test_up_epoxy_ctl_port_env_honored(compose_calls, cold, monkeypatch):
    monkeypatch.setenv("EPOXY_CTL_PORT", "8450")
    result = run_cli(["up", "--provider", "surfshark"])
    assert result.exit_code == 0
    assert read_registry("epoxy")["control_port"] == 8450


@pytest.mark.parametrize("value", ["0", "-5", "65536", "70000", "99999999999999999999"])
def test_up_epoxy_ctl_port_out_of_range_is_a_usage_error(value, compose_calls, cold, monkeypatch):
    """The env twin of --ctl-port gets the same 1..65535 bounds, as exit 2."""
    monkeypatch.setenv("EPOXY_CTL_PORT", value)
    result = run_cli(["up", "--provider", "surfshark"])
    assert result.exit_code == 2
    assert "EPOXY_CTL_PORT" in result.output
    assert compose_calls == []  # rejected before touching docker


@pytest.mark.parametrize("value", ["abc", "84a50", " "])
def test_up_epoxy_ctl_port_non_numeric_is_a_usage_error(value, compose_calls, cold, monkeypatch):
    monkeypatch.setenv("EPOXY_CTL_PORT", value)
    result = run_cli(["up", "--provider", "surfshark"])
    assert result.exit_code == 2
    assert compose_calls == []


def test_up_empty_epoxy_ctl_port_means_unset(compose_calls, cold, monkeypatch):
    """An empty value is absent, not invalid: it falls through to allocation."""
    monkeypatch.setenv("EPOXY_CTL_PORT", "")
    monkeypatch.setattr("epoxy.commands.up.allocate_free_port", lambda: 8123)
    result = run_cli(["up", "--provider", "surfshark"])
    assert result.exit_code == 0
    assert read_registry("epoxy")["control_port"] == 8123


@pytest.mark.parametrize(
    ("value", "valid"),
    [("1", True), ("8000", True), ("65535", True), ("0", False), ("65536", False), ("-1", False)],
)
def test_ctl_port_flag_and_env_share_the_same_bounds(value, valid, monkeypatch):
    """Guard against --ctl-port and its env twin drifting apart again."""
    flag = {p.name: p for p in cli.main.commands["up"].params}["ctl_port"]
    assert isinstance(flag.type, click.IntRange)
    assert (flag.type.min, flag.type.max) == (config.PORT_MIN, config.PORT_MAX)
    monkeypatch.setenv("EPOXY_CTL_PORT", value)
    if valid:
        assert _common._resolve_for_command("epoxy").control_port == int(value)
    else:
        with pytest.raises(click.UsageError, match="between"):
            _common._resolve_for_command("epoxy")


def test_up_running_without_registry_adopts_published_port(monkeypatch):
    """A container not owned by a registry record stays addressable via its port."""
    monkeypatch.setattr(docker, "container_running", lambda name=None: True)
    monkeypatch.setattr(docker, "container_control_port", lambda name=None: 8123)
    monkeypatch.setattr(
        _common,
        "effective_selection",
        lambda: Selection("surfshark", "wireguard", "Germany"),
    )
    result = run_cli(["up", "--instance", "plan-a"])
    assert result.exit_code == 0
    assert not (config.INSTANCES_DIR / "plan-a.json").exists()  # still not epoxy-owned
    body = (config.INSTANCES_DIR / "plan-a" / "compose.yml").read_text()
    assert "127.0.0.1:8123:8000/tcp" in body


def test_up_env_file_replaces_dotenv(compose_calls, cold, monkeypatch, tmp_path):
    env_file = tmp_path / "plan.env"
    env_file.write_text("HTTP_CONTROL_SERVER_API_KEY=from-file\nPLAN_ONLY=1\n")
    monkeypatch.setenv("HTTP_CONTROL_SERVER_API_KEY", "from-process")
    result = run_cli(
        ["up", "--instance", "plan-a", "--env-file", str(env_file), "--provider", "surfshark"]
    )
    assert result.exit_code == 0
    assert read_registry("plan-a")["env_file"] == str(env_file)


def test_up_ctl_port_override_persists_over_stale_record(compose_calls, monkeypatch):
    """An explicit --ctl-port on a running instance is remembered, not lost.

    The compose file is written with the resolved port on every `up`, so the
    registry must follow it or every later command resolves the stale port.
    """
    config.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    (config.INSTANCES_DIR / "plan-b.json").write_text(
        json.dumps({"instance": "plan-b", "control_port": 8000, "env_file": None})
    )
    monkeypatch.setattr(docker, "container_running", lambda name=None: True)
    monkeypatch.setattr(
        _common,
        "effective_selection",
        lambda: Selection("surfshark", "wireguard", "Germany"),
    )
    result = run_cli(["up", "--instance", "plan-b", "--ctl-port", "8399"])
    assert result.exit_code == 0
    assert read_registry("plan-b")["control_port"] == 8399
    body = (config.INSTANCES_DIR / "plan-b" / "compose.yml").read_text()
    assert "127.0.0.1:8399:8000/tcp" in body


def test_up_ctl_port_env_override_persists_over_stale_record(compose_calls, monkeypatch):
    """EPOXY_CTL_PORT is the env twin of --ctl-port, so it persists the same way."""
    config.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    (config.INSTANCES_DIR / "plan-b.json").write_text(
        json.dumps({"instance": "plan-b", "control_port": 8000, "env_file": None})
    )
    monkeypatch.setenv("EPOXY_CTL_PORT", "8450")
    monkeypatch.setattr(docker, "container_running", lambda name=None: True)
    monkeypatch.setattr(
        _common,
        "effective_selection",
        lambda: Selection("surfshark", "wireguard", "Germany"),
    )
    result = run_cli(["up", "--instance", "plan-b"])
    assert result.exit_code == 0
    assert read_registry("plan-b")["control_port"] == 8450


def test_up_without_registry_does_not_create_a_record(compose_calls, monkeypatch):
    """sync_registry only updates: an imported container stays registry-less."""
    monkeypatch.setattr(docker, "container_running", lambda name=None: True)
    monkeypatch.setattr(docker, "container_control_port", lambda name=None: 8123)
    monkeypatch.setattr(
        _common,
        "effective_selection",
        lambda: Selection("surfshark", "wireguard", "Germany"),
    )
    result = run_cli(["up", "--instance", "plan-a"])
    assert result.exit_code == 0
    assert not (config.INSTANCES_DIR / "plan-a.json").exists()
    body = (config.INSTANCES_DIR / "plan-a" / "compose.yml").read_text()
    assert "127.0.0.1:8123:8000/tcp" in body


def test_up_invalid_instance_name_exits_2():
    result = run_cli(["up", "--instance", "bad name"])
    assert result.exit_code == 2


def test_instance_options_present_on_commands():
    for name in ["status", "connect", "down", "rm", "logs", "bench", "dns", "update"]:
        cmd = cli.main.commands[name]
        assert any(p.name == "instance" for p in cmd.params), name
    up_params = {p.name for p in cli.main.commands["up"].params}
    assert {"instance", "ctl_port", "env_file"} <= up_params


def test_up_exit_1_when_not_verified(compose_calls, cold, monkeypatch):
    monkeypatch.setattr(ipinfo, "print_ip_status", lambda **kwargs: False)
    result = run_cli(["up", "--provider", "surfshark"])
    assert result.exit_code == 1


def test_connect_exit_1_when_not_verified(monkeypatch):
    monkeypatch.setattr(ipinfo, "print_ip_status", lambda **kwargs: False)
    monkeypatch.setattr(docker, "container_running", lambda: True)
    monkeypatch.setattr(
        _common,
        "effective_selection",
        lambda: Selection("surfshark", "wireguard", "Germany"),
    )
    monkeypatch.setattr(apply, "apply_location", lambda sel: None)
    result = run_cli(["connect", "--country", "France"])
    assert result.exit_code == 1


def test_up_unknown_provider_is_friendly_not_traceback(compose_calls, cold):
    """A provider typo must produce the friendly error, never a KeyError traceback."""
    result = run_cli(["up", "--provider", "sufshark"])
    assert result.exit_code == 1
    assert "Unknown provider 'sufshark'" in result.output
    assert "Available: protonvpn, surfshark" in result.output
    assert "Traceback" not in result.output
    assert compose_calls == []


def test_commands_require_instance(compose_calls, cold, monkeypatch):
    """No --instance is a usage error, not a hidden default."""
    monkeypatch.setattr(_common, "_stdin_is_tty", lambda: False)
    result = run_bare_cli(["up", "--provider", "surfshark"])
    assert result.exit_code == 2
    assert "--instance" in result.output
    assert compose_calls == []


# ---------------------------------------------------------------------------
# interactive instance choice (no --instance, no env, TTY)
# ---------------------------------------------------------------------------


def _multi_instance(monkeypatch):
    monkeypatch.setattr(_common, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(discovery, "known_names", lambda: {"plan-a", "plan-b"})
    monkeypatch.setattr(discovery, "instance_state", lambda name: "running")


def test_choose_instance_non_tty_is_usage_error(monkeypatch):
    monkeypatch.setattr(_common, "_stdin_is_tty", lambda: False)
    with pytest.raises(click.UsageError, match="--instance"):
        _common._choose_instance_name()


def test_choose_instance_zero_known_is_usage_error(monkeypatch):
    monkeypatch.setattr(_common, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(discovery, "known_names", lambda: set())
    with pytest.raises(click.UsageError, match="--instance"):
        _common._choose_instance_name()


def test_choose_instance_auto_uses_sole_instance(monkeypatch):
    monkeypatch.setattr(_common, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(discovery, "known_names", lambda: {"plan-a"})
    assert _common._choose_instance_name() == "plan-a"


def test_choose_instance_prompts_when_multiple(monkeypatch):
    _multi_instance(monkeypatch)
    picked: list[list[tuple[str, str]]] = []

    def fake_pick(instances: list[tuple[str, str]], **kw: object) -> str:
        picked.append(instances)
        return "plan-b"

    monkeypatch.setattr(picker, "select_instance", fake_pick)
    assert _common._choose_instance_name() == "plan-b"
    assert picked == [[("plan-a", "running"), ("plan-b", "running")]]


def test_choose_instance_cancel_is_error(monkeypatch):
    _multi_instance(monkeypatch)
    monkeypatch.setattr(picker, "select_instance", lambda instances, **kw: None)
    with pytest.raises(click.ClickException, match="No instance selected"):
        _common._choose_instance_name()


def test_down_picks_instance_when_multiple(monkeypatch):
    """The chosen instance flows through instance_context: down targets its project."""
    _multi_instance(monkeypatch)
    monkeypatch.setattr(picker, "select_instance", lambda instances, **kw: "plan-a")
    monkeypatch.setattr("epoxy.control.set_tunnel_status", lambda *a, **kw: None)
    projects: list[str] = []

    def fake_compose(*args: str, env_overrides=None, timeout=None) -> CompletedProcess[str]:
        projects.append(current_instance().project)
        return CompletedProcess((), 0)

    monkeypatch.setattr("epoxy.docker.compose", fake_compose)
    result = run_bare_cli(["down"])
    assert result.exit_code == 0
    assert projects == ["epoxy-plan-a"]


def test_status_picks_instance_once_when_multiple(monkeypatch):
    """Single `status` must prompt exactly once (reuse _resolve_targets)."""
    _multi_instance(monkeypatch)
    picks: list[list[tuple[str, str]]] = []

    def fake_pick(instances: list[tuple[str, str]], **kw: object) -> str:
        picks.append(instances)
        return "plan-a"

    monkeypatch.setattr(picker, "select_instance", fake_pick)
    seen: list[str] = []
    monkeypatch.setattr(
        "epoxy.commands.status._print_human_status",
        lambda size, no_speedtest: seen.append(current_instance().name),
    )
    result = run_bare_cli(["status", "--no-speedtest"])
    assert result.exit_code == 0
    assert len(picks) == 1
    assert seen == ["plan-a"]


def test_epoxy_instance_env_is_ignored(compose_calls, cold, monkeypatch):
    """The removed EPOXY_INSTANCE knob must stay dead: setting it changes nothing."""
    monkeypatch.setenv("EPOXY_INSTANCE", "bogus")
    monkeypatch.setattr(_common, "_stdin_is_tty", lambda: False)
    result = run_bare_cli(["up", "--provider", "surfshark"])
    assert result.exit_code == 2
    assert "--instance" in result.output
    assert compose_calls == []


def test_version_flag_reports_exact_package_version():
    result = run_cli(["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"epoxy {__version__}"


def test_pyproject_version_matches_version_module():
    tomllib = pytest.importorskip("tomllib")
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__
