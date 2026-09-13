"""Tests for instance discovery (vpn ls) and its --json schema."""

import json
from subprocess import CompletedProcess

import pytest
from click.testing import CliRunner

from vpn import cli, discovery
from vpn.apply import Selection


def _proc(stdout: str) -> CompletedProcess[str]:
    return CompletedProcess((), 0, stdout=stdout)


def _stub_docker(runner, monkeypatch):
    """Return a runner for vpn.discovery.run keyed on its --format argument."""

    def fake_run(*args, **kwargs):
        fmt = args[args.index("--format") + 1]
        if "NetworkMode" in fmt:
            return _proc(
                "consumer-a\tcontainer:gluetun\n"
                "plan-app\tcontainer:plan-a\n"
                "unrelated\tfile:///app/compose.yml\n"
            )
        if "compose.project" in fmt:
            return _proc("vpn-gluetun\nvpn-plan-a\nother-app\n")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(discovery, "run", fake_run)
    return fake_run


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("HTTP_CONTROL_SERVER_API_KEY", "test-key")


def test_state_mapping(monkeypatch):
    for status, expected in [("running", "running"), ("restarting", "starting"),
                             ("exited", "stopped"), ("created", "stopped")]:
        monkeypatch.setattr(discovery, "container_status", lambda n, status=status: status)
        assert discovery._state("gluetun") == expected
    monkeypatch.setattr(discovery, "container_status", lambda n: None)
    assert discovery._state("gluetun") == "absent"


def test_consumers_of(monkeypatch):
    _stub_docker(None, monkeypatch)
    assert discovery.consumers_of("gluetun") == ["consumer-a"]
    assert discovery.consumers_of("plan-a") == ["plan-app"]
    assert discovery.consumers_of("other") == []


def test_known_names_from_registry_and_projects(monkeypatch):
    _stub_docker(None, monkeypatch)
    from vpn import config
    (config.INSTANCES_DIR / "from-registry.json").parent.mkdir(parents=True, exist_ok=True)
    (config.INSTANCES_DIR / "from-registry.json").write_text("{}")
    assert discovery._known_names() == {"gluetun", "plan-a", "from-registry"}


def test_instance_records_schema(monkeypatch):
    _stub_docker(None, monkeypatch)
    monkeypatch.setattr(discovery, "container_status", lambda n: "running")
    monkeypatch.setattr(discovery, "container_control_port", lambda n: 8123)
    monkeypatch.setattr(
        "vpn.control.get_settings",
        lambda: {"type": "wireguard", "provider": {"name": "surfshark"},
                 "server_selection": {}},
    )
    records = discovery.instance_records()
    assert [r["instance"] for r in records] == ["gluetun", "plan-a"]
    plan = records[1]
    assert plan == {
        "instance": "plan-a",
        "container_name": "plan-a",
        "state": "running",
        "selection": {
            "provider": "surfshark",
            "protocol": "wireguard",
            "country": None,
            "city": None,
        },
        "control_server": {"port": 8123, "enabled": True},
        "consumers": ["plan-app"],
    }


def test_selection_doc_schema():
    sel = Selection("surfshark", "wireguard", "Japan", "Tokyo")
    assert discovery.selection_doc(sel) == {
        "provider": "surfshark",
        "protocol": "wireguard",
        "country": "Japan",
        "city": "Tokyo",
    }


def test_ls_json_envelope(monkeypatch):
    monkeypatch.setattr(
        cli,
        "instance_records",
        lambda: [{
            "instance": "plan-a",
            "container_name": "plan-a",
            "state": "stopped",
            "selection": None,
            "control_server": {"port": 8123, "enabled": False},
            "consumers": [],
        }],
    )
    result = CliRunner().invoke(cli.main, ["ls", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert set(doc) == {"default", "instances"}
    assert doc["default"] == "gluetun"
    assert doc["instances"][0]["instance"] == "plan-a"


def test_ls_json_filters_by_instance(monkeypatch):
    monkeypatch.setattr(
        cli,
        "instance_records",
        lambda: [{
            "instance": "plan-a",
            "container_name": "plan-a",
            "state": "stopped",
            "selection": None,
            "control_server": {"port": 8123, "enabled": False},
            "consumers": [],
        }],
    )
    result = CliRunner().invoke(
        cli.main, ["ls", "--json", "--instance", "other"], catch_exceptions=False
    )
    assert json.loads(result.output) == {"default": "gluetun", "instances": []}


def test_ls_human_prints_table(monkeypatch):
    monkeypatch.setattr(cli, "instance_records", lambda: [_record()])
    result = CliRunner().invoke(cli.main, ["ls"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "plan-a" in result.output
    assert "INSTANCE" in result.output


def _record() -> dict[str, object]:
    return {
        "instance": "plan-a",
        "container_name": "plan-a",
        "state": "running",
        "selection": {
            "provider": "surfshark",
            "protocol": "wireguard",
            "country": "Japan",
            "city": "Tokyo",
        },
        "control_server": {"port": 8123, "enabled": True},
        "consumers": ["plan-app"],
    }
