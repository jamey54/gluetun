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
    required_name,
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


def test_parse_instance_name_trims_padding():
    assert parse_instance_name("  plan-a  ") == "plan-a"


def test_required_name_from_explicit(monkeypatch):
    monkeypatch.delenv("GLUETUN_INSTANCE", raising=False)
    assert required_name("plan-a") == "plan-a"


def test_required_name_from_env(monkeypatch):
    monkeypatch.setenv("GLUETUN_INSTANCE", "plan-a")
    assert required_name(None) == "plan-a"


def test_required_name_without_source_exits(monkeypatch):
    """No --instance and no GLUETUN_INSTANCE is a usage error, not a hidden default."""
    monkeypatch.delenv("GLUETUN_INSTANCE", raising=False)
    with pytest.raises(click.UsageError, match="GLUETUN_INSTANCE"):
        required_name(None)


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


def test_every_instance_shares_the_cwd_env_file(tmp_path, monkeypatch):
    """The shared credential source is ./.env for every instance."""
    monkeypatch.chdir(tmp_path)
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


def test_compose_file_for_always_generated(tmp_path, monkeypatch):
    assert compose_file_for("gluetun") == str(config.INSTANCES_DIR / "gluetun" / "compose.yml")
    assert compose_file_for("plan-a") == str(config.INSTANCES_DIR / "plan-a" / "compose.yml")


def test_render_compose_swaps_name_and_port():
    body = render_compose("plan-a", 8123)
    assert "container_name: plan-a" in body
    assert "127.0.0.1:8123:8000/tcp" in body
    assert "container_name: gluetun" not in body


def test_ensure_compose_file_generates_for_every_instance():
    default = resolve_instance("gluetun")
    ensure_compose_file(default)
    assert (config.INSTANCES_DIR / "gluetun" / "compose.yml").exists()

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


def test_allocate_free_port_prefers_lowest_free(monkeypatch):
    taken = {7999, 8000, 8001}  # 8000/8001 busy -> first free is 8002
    monkeypatch.setattr("vpn.instance._port_in_use", lambda p: p in taken)
    assert allocate_free_port() == 8002


def test_allocate_free_port_full_range_is_friendly_error(monkeypatch):
    monkeypatch.setattr("vpn.instance._port_in_use", lambda p: True)
    with pytest.raises(SystemExit, match="--ctl-port"):
        allocate_free_port()


def test_registry_json_matches_documented_schema():
    write_registry(resolve_instance("plan-a", control_port=8123, env_file="/tmp/e"))
    data = json.loads((config.INSTANCES_DIR / "plan-a.json").read_text())
    assert set(data) == {"instance", "control_port", "env_file"}
