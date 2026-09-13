"""Unit tests for vpn.instance: naming, registry, env, compose, ports."""

import json
import socket

import click
import pytest

from vpn import config
from vpn.instance import (
    Instance,
    allocate_free_port,
    compose_file_for,
    ensure_compose_file,
    list_registry,
    parse_instance_name,
    read_registry,
    render_compose,
    resolve_default_name,
    resolve_instance,
    write_registry,
)


def test_parse_instance_name_accepts_docker_safe_names():
    for good in ["gluetun", "plan-a", "plan_1", "a.b", "A1", "a-b-c", "0x"]:
        assert parse_instance_name(good) == good


def test_parse_instance_name_rejects_unsafe_names():
    for bad in ["", "bad name", "a b", "ä-ö", "-lead", ".dot", "a:bc", "a/b"]:
        with pytest.raises(click.UsageError):
            parse_instance_name(bad)


def test_resolve_default_name_env_alias(monkeypatch):
    assert resolve_default_name() == "gluetun"
    monkeypatch.setenv("GLUETUN_INSTANCE", "plan-a")
    assert resolve_default_name() == "plan-a"


def test_instance_properties():
    inst = Instance("plan-a", 8123, None, {}, "/cfg/compose.yml")
    assert inst.container == "plan-a"
    assert inst.project == "vpn-plan-a"
    assert inst.lock_file.endswith("locks/plan-a.lock")
    assert inst.base_url == "http://127.0.0.1:8123"


def test_build_env_env_file_overrides_process(tmp_path, monkeypatch):
    monkeypatch.setenv("FOO", "from-process")
    monkeypatch.setenv("BAR", "keep")
    env_file = tmp_path / "plan.env"
    env_file.write_text("FOO=from-file\nBAZ=z\n")
    inst = resolve_instance("plan-a", control_port=8123, env_file=str(env_file))
    assert inst.env["FOO"] == "from-file"
    assert inst.env["BAR"] == "keep"
    assert inst.env["BAZ"] == "z"


def test_non_default_instances_share_the_default_env_file(tmp_path, monkeypatch):
    """Section 4: dedicated instances still read credentials from the shared .env."""
    monkeypatch.setenv("GLUETUN_COMPOSE_FILE", str(tmp_path / "compose.yml"))
    (tmp_path / ".env").write_text("SURFSHARK_WIREGUARD_PRIVATE_KEY=shared-key\n")
    inst = resolve_instance("plan-a", control_port=8123)
    assert inst.env["SURFSHARK_WIREGUARD_PRIVATE_KEY"] == "shared-key"


def test_registry_round_trip():
    inst = resolve_instance("plan-a", control_port=8123)
    write_registry(inst)
    assert read_registry("plan-a") == {
        "instance": "plan-a",
        "control_port": 8123,
        "env_file": None,
    }
    assert list_registry() == ["plan-a"]


def test_registry_records_env_file():
    inst = resolve_instance("plan-a", control_port=8123, env_file="/tmp/plan.env")
    write_registry(inst)
    stored = read_registry("plan-a")
    assert stored is not None and stored["env_file"] == "/tmp/plan.env"
    reported = resolve_instance("plan-a").env_file
    assert reported is not None and str(reported) == "/tmp/plan.env"


def test_compose_file_for(tmp_path, monkeypatch):
    custom = tmp_path / "custom-compose.yml"
    monkeypatch.setenv("GLUETUN_COMPOSE_FILE", str(custom))
    assert compose_file_for("gluetun") == str(custom)
    assert compose_file_for("plan-a") == str(
        config.INSTANCES_DIR / "plan-a" / "compose.yml"
    )


def test_render_compose_swaps_name_and_port():
    body = render_compose("plan-a", 8123)
    assert "container_name: plan-a" in body
    assert "127.0.0.1:8123:8000/tcp" in body
    assert "container_name: gluetun" not in body


def test_ensure_compose_file_only_generates_for_non_default(tmp_path, monkeypatch):
    monkeypatch.setenv("GLUETUN_COMPOSE_FILE", str(tmp_path / "cf.yml"))
    default = resolve_instance("gluetun")
    ensure_compose_file(default)
    assert not (config.INSTANCES_DIR / "gluetun" / "compose.yml").exists()

    named = resolve_instance("plan-a", control_port=8123)
    ensure_compose_file(named)
    assert (config.INSTANCES_DIR / "plan-a" / "compose.yml").exists()


def test_resolve_instance_uses_registry_port():
    write_registry(resolve_instance("plan-a", control_port=8123))
    assert resolve_instance("plan-a").control_port == 8123


def test_allocate_free_port_returns_bindable_port():
    port = allocate_free_port()
    assert 8000 <= port <= 9000
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_registry_json_matches_documented_schema():
    write_registry(resolve_instance("plan-a", control_port=8123, env_file="/tmp/e"))
    data = json.loads((config.INSTANCES_DIR / "plan-a.json").read_text())
    assert set(data) == {"instance", "control_port", "env_file"}
