"""Tests for instance discovery (vpn ls) and its --json schema."""

import json
from subprocess import CompletedProcess

import pytest
from click.testing import CliRunner

from epoxy import cli, config, discovery
from epoxy.apply import Selection


def _proc(stdout: str) -> CompletedProcess[str]:
    return CompletedProcess((), 0, stdout=stdout)


_STUB_ID = "c0f2a1b3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"
_STUB_STARTED = "2026-09-19T09:00:00.123456789Z"


def _stub_docker(runner, monkeypatch):
    """Return a runner for epoxy.discovery.run keyed on its --format argument."""
    monkeypatch.setattr(discovery, "container_id", lambda name: _STUB_ID)
    monkeypatch.setattr(discovery, "container_started_at", lambda name: _STUB_STARTED)

    def fake_run(*args, **kwargs):
        fmt = args[args.index("--format") + 1]
        if "NetworkMode" in fmt:
            return _proc(
                "consumer-a\tcontainer:epoxy\n"
                "plan-app\tcontainer:plan-a\n"
                "unrelated\tfile:///app/compose.yml\n"
            )
        if "compose.project" in fmt:
            return _proc("epoxy-epoxy\nepoxy-plan-a\nother-app\n")
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(discovery, "run", fake_run)
    return fake_run


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("HTTP_CONTROL_SERVER_API_KEY", "test-key")


def test_state_mapping(monkeypatch):
    for status, expected in [
        ("running", "running"),
        ("restarting", "starting"),
        ("exited", "stopped"),
        ("created", "stopped"),
    ]:
        monkeypatch.setattr(discovery, "container_status", lambda n, status=status: status)
        assert discovery._state("epoxy") == expected
    monkeypatch.setattr(discovery, "container_status", lambda n: None)
    assert discovery._state("epoxy") == "absent"


def test_consumers_of(monkeypatch):
    _stub_docker(None, monkeypatch)
    assert discovery.consumers_of("epoxy") == ["consumer-a"]
    assert discovery.consumers_of("plan-a") == ["plan-app"]
    assert discovery.consumers_of("other") == []


def test_consumers_of_sorted_across_many(monkeypatch):
    def fake_run(*args, **kwargs):
        return _proc("z-app\tcontainer:epoxy\na-app\tcontainer:epoxy\nb-app\tcontainer:epoxy\n")

    monkeypatch.setattr(discovery, "run", fake_run)
    assert discovery.consumers_of("epoxy") == ["a-app", "b-app", "z-app"]


def test_consumers_of_matches_instance_by_full_and_short_id(monkeypatch):
    """Docker records ``container:<name>`` as ``container:<id>`` at attach time,
    so consumers must also match the instance's full and short container ID."""
    full = "c0f2a1b3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"
    monkeypatch.setattr(discovery, "container_id", lambda name: full)
    monkeypatch.setattr(
        discovery,
        "run",
        lambda *args, **kwargs: _proc(
            f"app-full\tcontainer:{full}\n"
            f"app-short\tcontainer:{full[:12]}\n"
            "other\tcontainer:deadbeef\n"
        ),
    )
    assert discovery.consumers_of("plan-a") == ["app-full", "app-short"]


def test_consumers_of_absent_container_matches_name_only(monkeypatch):
    """No container -> no IDs; consumers (if any) can only match by name."""
    monkeypatch.setattr(discovery, "container_id", lambda name: None)
    monkeypatch.setattr(
        discovery,
        "run",
        lambda *args, **kwargs: _proc("app-a\tcontainer:plan-a\n"),
    )
    assert discovery.consumers_of("plan-a") == ["app-a"]


def test_consumers_of_missing_docker_reads_as_empty(monkeypatch):
    monkeypatch.setattr(
        discovery, "run", lambda *a, **kw: CompletedProcess(a, 127, stderr="no docker")
    )
    assert discovery.consumers_of("epoxy") == []
    assert discovery._compose_projects() == []


def test_docker_ps_calls_are_bounded(monkeypatch):
    from epoxy import config

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        discovery,
        "run",
        lambda *args, **kw: seen.update(kw) or _proc(""),
    )
    discovery.consumers_of("epoxy")
    assert seen["timeout"] == config.CONTAINER_OP_TIMEOUT_S


def test_known_names_from_registry_and_projects(monkeypatch):
    _stub_docker(None, monkeypatch)
    from epoxy import config

    (config.INSTANCES_DIR / "from-registry.json").parent.mkdir(parents=True, exist_ok=True)
    (config.INSTANCES_DIR / "from-registry.json").write_text("{}")
    assert discovery._known_names() == {"epoxy", "plan-a", "from-registry"}


def test_instance_records_schema(monkeypatch):
    _stub_docker(None, monkeypatch)
    monkeypatch.setattr(discovery, "container_status", lambda n: "running")
    monkeypatch.setattr(discovery, "container_control_port", lambda n: 8123)
    monkeypatch.setattr(
        "epoxy.control.get_settings",
        lambda: {"type": "wireguard", "provider": {"name": "surfshark"}, "server_selection": {}},
    )
    records = discovery.instance_records()
    assert [r["instance"] for r in records] == ["epoxy", "plan-a"]
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
        "started_at": _STUB_STARTED,
    }


def test_selection_doc_schema():
    sel = Selection("surfshark", "wireguard", "Japan", "Tokyo")
    assert discovery.selection_doc(sel) == {
        "provider": "surfshark",
        "protocol": "wireguard",
        "country": "Japan",
        "city": "Tokyo",
    }


def test_record_without_control_port_has_null_control_server(monkeypatch):
    """A running container with no registered or published port reports None,
    not a crash or a misleading port."""
    (config.INSTANCES_DIR / "plan-a.json").parent.mkdir(parents=True, exist_ok=True)
    (config.INSTANCES_DIR / "plan-a.json").write_text('{"instance": "plan-a"}')
    monkeypatch.setattr(discovery, "container_status", lambda n: "running")
    monkeypatch.setattr(discovery, "container_control_port", lambda n: None)
    monkeypatch.setattr(
        "epoxy.control.get_settings",
        lambda: {"type": "wireguard", "provider": {"name": "surfshark"}, "server_selection": {}},
    )
    record = next(r for r in discovery.instance_records() if r["instance"] == "plan-a")
    assert record["control_server"] is None


def test_ls_json_envelope(monkeypatch):
    monkeypatch.setattr(
        cli,
        "instance_records",
        lambda: [
            {
                "instance": "plan-a",
                "container_name": "plan-a",
                "state": "stopped",
                "selection": None,
                "control_server": {"port": 8123, "enabled": False},
                "consumers": [],
                "started_at": _STUB_STARTED,
            }
        ],
    )
    result = CliRunner().invoke(cli.main, ["ls", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert set(doc) == {"instances"}
    assert doc["instances"][0]["instance"] == "plan-a"
    assert doc["instances"][0]["started_at"] == _STUB_STARTED


def test_ls_json_filters_by_instance(monkeypatch):
    monkeypatch.setattr(
        cli,
        "instance_records",
        lambda: [
            {
                "instance": "plan-a",
                "container_name": "plan-a",
                "state": "stopped",
                "selection": None,
                "control_server": {"port": 8123, "enabled": False},
                "consumers": [],
                "started_at": _STUB_STARTED,
            }
        ],
    )
    result = CliRunner().invoke(
        cli.main, ["ls", "--json", "--instance", "other"], catch_exceptions=False
    )
    assert json.loads(result.output) == {"instances": []}


def test_ls_human_prints_table(monkeypatch):
    monkeypatch.setattr(cli, "instance_records", lambda: [_record()])
    result = CliRunner().invoke(cli.main, ["ls"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "plan-a" in result.output
    assert "INSTANCE" in result.output
    assert "STARTED" in result.output
    assert discovery._local_started(_STUB_STARTED) in result.output


def test_ls_human_no_instances_message(monkeypatch):
    monkeypatch.setattr(cli, "instance_records", lambda: [])
    result = CliRunner().invoke(cli.main, ["ls"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "(no instances)" in result.output


def test_ls_human_filters_by_instance(monkeypatch):
    monkeypatch.setattr(cli, "instance_records", lambda: [_record()])
    result = CliRunner().invoke(cli.main, ["ls", "--instance", "other"], catch_exceptions=False)
    assert "(no instances)" in result.output
    result = CliRunner().invoke(cli.main, ["ls", "--instance", "plan-a"], catch_exceptions=False)
    assert "plan-a" in result.output


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
        "started_at": _STUB_STARTED,
    }


def test_instance_records_ordered_oldest_first(monkeypatch):
    """ls records are ordered by start time (older instance first)."""
    times = {
        "epoxy": "2026-01-01T08:00:00Z",
        "plan-b": "2026-05-01T08:00:00Z",
        "plan-a": "2026-03-01T08:00:00Z",
    }
    monkeypatch.setattr(discovery, "_known_names", lambda: set(times))
    monkeypatch.setattr(discovery, "_state", lambda name: "running")
    monkeypatch.setattr(discovery, "_control_port", lambda name: 8000)
    monkeypatch.setattr(discovery, "_runtime_selection", lambda name, port: None)
    monkeypatch.setattr(discovery, "container_started_at", lambda name: times[name])
    monkeypatch.setattr(discovery, "consumers_of", lambda name: [])
    records = discovery.instance_records()
    assert [r["instance"] for r in records] == ["epoxy", "plan-a", "plan-b"]
    assert [r["started_at"] for r in records] == [
        "2026-01-01T08:00:00Z",
        "2026-03-01T08:00:00Z",
        "2026-05-01T08:00:00Z",
    ]


def test_instance_records_unknown_started_sorts_last(monkeypatch):
    """Absent/never-started instances (no start time) sort after started ones."""
    monkeypatch.setattr(discovery, "_known_names", lambda: {"new-one", "old-one"})
    monkeypatch.setattr(
        discovery, "_state", lambda name: "running" if name == "old-one" else "absent"
    )
    monkeypatch.setattr(discovery, "_control_port", lambda name: 8000)
    monkeypatch.setattr(discovery, "_runtime_selection", lambda name, port: None)
    monkeypatch.setattr(
        discovery,
        "container_started_at",
        lambda name: "2026-01-01T08:00:00Z" if name == "old-one" else None,
    )
    monkeypatch.setattr(discovery, "consumers_of", lambda name: [])
    records = discovery.instance_records()
    assert [r["instance"] for r in records] == ["old-one", "new-one"]
    assert [r["started_at"] for r in records] == ["2026-01-01T08:00:00Z", None]


def test_instance_records_absent_instance_reports_null_start(monkeypatch):
    """Absent containers carry no start time (registry-only records stay clean)."""
    monkeypatch.setattr(discovery, "_known_names", lambda: {"plan-a"})
    monkeypatch.setattr(discovery, "_state", lambda name: "absent")
    monkeypatch.setattr(discovery, "container_started_at", lambda name: pytest.fail("not called"))
    monkeypatch.setattr(discovery, "consumers_of", lambda name: [])
    record = discovery.instance_records()[0]
    assert record["started_at"] is None


def test_local_started_formats_utc_to_local():
    rendered = discovery._local_started("2026-09-19T09:00:00Z")
    assert rendered.endswith(" 09:00:00")
    assert discovery._local_started("not-a-time") == "not-a-time"
