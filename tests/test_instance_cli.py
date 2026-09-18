"""CLI tests for the per-instance surface: naming, ports, env files, exit codes."""

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from click.testing import CliRunner

from vpn import cli, config
from vpn.apply import Selection
from vpn.version import __version__


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("SURFSHARK_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("PROTONVPN_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("PROTONVPN_WIREGUARD_ADDRESSES", "10.2.0.2/32")
    monkeypatch.setenv("HTTP_CONTROL_SERVER_API_KEY", "test-key")
    monkeypatch.setattr("vpn.cli.print_ip_status", lambda **kwargs: True)
    monkeypatch.setattr("vpn.cli.measure", lambda size=25: None)
    monkeypatch.setattr("vpn.cli.current_exit_ip", lambda: None)


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

    monkeypatch.setattr("vpn.cli.compose", fake_compose)
    return calls


@pytest.fixture()
def cold(monkeypatch):
    monkeypatch.setattr(cli, "container_running", lambda name=None: False)


def invoke(args):
    return CliRunner().invoke(cli.main, args, catch_exceptions=False)


def read_registry(name: str) -> dict[str, object]:
    return json.loads((config.INSTANCES_DIR / f"{name}.json").read_text())


def test_up_non_default_instance_writes_compose_and_registry(compose_calls, cold, monkeypatch):
    monkeypatch.setattr("vpn.cli.allocate_free_port", lambda: 8123)
    result = invoke(["up", "--instance", "plan-a", "--provider", "surfshark"])
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
    monkeypatch.setattr("vpn.cli.allocate_free_port", lambda: pytest.fail("must not allocate"))
    result = invoke(["up", "--instance", "plan-a", "--provider", "surfshark"])
    assert result.exit_code == 0
    body = (config.INSTANCES_DIR / "plan-a" / "compose.yml").read_text()
    assert "127.0.0.1:8123:8000/tcp" in body
    assert read_registry("plan-a")["control_port"] == 8123


def test_up_ctl_port_wins_over_allocation(compose_calls, cold, monkeypatch):
    monkeypatch.setattr("vpn.cli.allocate_free_port", lambda: pytest.fail("must not allocate"))
    result = invoke(["up", "--instance", "plan-b", "--ctl-port", "8300", "--provider", "surfshark"])
    assert result.exit_code == 0
    assert read_registry("plan-b")["control_port"] == 8300
    assert (
        "127.0.0.1:8300:8000/tcp" in (config.INSTANCES_DIR / "plan-b" / "compose.yml").read_text()
    )


def test_up_gluetun_ctl_port_env_honored(compose_calls, cold, monkeypatch):
    monkeypatch.setenv("GLUETUN_CTL_PORT", "8450")
    result = invoke(["up", "--provider", "surfshark"])
    assert result.exit_code == 0
    assert read_registry("gluetun")["control_port"] == 8450


def test_up_running_without_registry_adopts_published_port(monkeypatch):
    """A container not owned by a registry record stays addressable via its port."""
    monkeypatch.setattr(cli, "container_running", lambda name=None: True)
    monkeypatch.setattr(cli, "container_control_port", lambda name=None: 8123)
    monkeypatch.setattr(
        cli,
        "effective_selection",
        lambda: Selection("surfshark", "wireguard", "Germany"),
    )
    result = invoke(["up", "--instance", "plan-a"])
    assert result.exit_code == 0
    assert not (config.INSTANCES_DIR / "plan-a.json").exists()  # still not vpn-owned
    body = (config.INSTANCES_DIR / "plan-a" / "compose.yml").read_text()
    assert "127.0.0.1:8123:8000/tcp" in body


def test_up_env_file_replaces_dotenv(compose_calls, cold, monkeypatch, tmp_path):
    env_file = tmp_path / "plan.env"
    env_file.write_text("HTTP_CONTROL_SERVER_API_KEY=from-file\nPLAN_ONLY=1\n")
    monkeypatch.setenv("HTTP_CONTROL_SERVER_API_KEY", "from-process")
    result = invoke(
        ["up", "--instance", "plan-a", "--env-file", str(env_file), "--provider", "surfshark"]
    )
    assert result.exit_code == 0
    assert read_registry("plan-a")["env_file"] == str(env_file)


def test_up_invalid_instance_name_exits_2():
    result = CliRunner().invoke(cli.main, ["up", "--instance", "bad name"])
    assert result.exit_code == 2


def test_instance_options_present_on_commands():
    for name in ["status", "connect", "down", "logs", "bench", "dns", "update"]:
        cmd = cli.main.commands[name]
        assert any(p.name == "instance" for p in cmd.params), name
    up_params = {p.name for p in cli.main.commands["up"].params}
    assert {"instance", "ctl_port", "env_file"} <= up_params


def test_up_exit_1_when_not_verified(compose_calls, cold, monkeypatch):
    monkeypatch.setattr(cli, "print_ip_status", lambda **kwargs: False)
    result = invoke(["up", "--provider", "surfshark"])
    assert result.exit_code == 1


def test_connect_exit_1_when_not_verified(monkeypatch):
    monkeypatch.setattr(cli, "print_ip_status", lambda **kwargs: False)
    monkeypatch.setattr(cli, "container_running", lambda: True)
    monkeypatch.setattr(
        cli,
        "effective_selection",
        lambda: Selection("surfshark", "wireguard", "Germany"),
    )
    monkeypatch.setattr(cli, "apply_location", lambda sel: None)
    result = invoke(["connect", "--country", "France"])
    assert result.exit_code == 1


def test_up_unknown_provider_is_friendly_not_traceback(compose_calls, cold):
    """A provider typo must produce the friendly error, never a KeyError traceback."""
    result = invoke(["up", "--provider", "sufshark"])
    assert result.exit_code == 1
    assert "Unknown provider 'sufshark'" in result.output
    assert "Available: protonvpn, surfshark" in result.output
    assert "Traceback" not in result.output
    assert compose_calls == []


def test_commands_require_instance_or_env(compose_calls, cold, monkeypatch):
    """No --instance and no GLUETUN_INSTANCE is a usage error, not a hidden default."""
    monkeypatch.delenv("GLUETUN_INSTANCE", raising=False)
    result = invoke(["up", "--provider", "surfshark"])
    assert result.exit_code == 2
    assert "GLUETUN_INSTANCE" in result.output
    assert compose_calls == []


def test_version_flag_reports_exact_package_version():
    result = invoke(["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"vpn {__version__}"


def test_pyproject_version_matches_version_module():
    tomllib = pytest.importorskip("tomllib")
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__
