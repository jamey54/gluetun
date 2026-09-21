"""Tests for machine-readable `status --json` (schema, exit codes)."""

import json

import pytest
from click.testing import CliRunner

from epoxy import cli, control, ipinfo
from epoxy.control import ControlError


def _settings(provider: str = "surfshark", country: str | None = None) -> dict[str, object]:
    return {
        "type": "wireguard",
        "provider": {
            "name": provider,
            "server_selection": {
                "countries": [country] if country else [],
                "cities": [],
            },
        },
    }


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("HTTP_CONTROL_SERVER_API_KEY", "test-key")


def invoke_status(
    monkeypatch,
    *,
    state: str = "running",
    leak: bool = False,
    control_error: str | None = None,
    baked_provider: str = "surfshark",
    baked_country: str = "Germany",
    runtime: dict[str, object] | None = None,
    probe_fail: bool = False,
    probe_geo: bool = True,
):
    monkeypatch.setattr(
        "epoxy.discovery.container_status",
        lambda n=None: None if state == "absent" else state,
    )
    monkeypatch.setattr(
        cli,
        "container_env",
        lambda: {
            "VPN_SERVICE_PROVIDER": baked_provider,
            "VPN_TYPE": "wireguard",
            "VPN_COUNTRY": baked_country,
        },
    )
    monkeypatch.setattr(cli, "container_image", lambda: "qmcgaw/gluetun:latest")
    if control_error:

        def raise_error():
            raise ControlError(500, control_error)

        monkeypatch.setattr(control, "get_settings", raise_error)
    else:
        monkeypatch.setattr(
            control, "get_settings", lambda: runtime or _settings(country="Germany")
        )
    probe_ip = "1.1.1.1" if leak else "9.9.9.9"
    if probe_fail:
        monkeypatch.setattr(cli, "_probe", lambda: None)
    elif not probe_geo:
        probe = ipinfo._Probe({"ip": probe_ip}, sources=("cloudflare",))
        monkeypatch.setattr(cli, "_probe", lambda: probe)
    else:
        probe = ipinfo._Probe({"ip": probe_ip, "country": "DE"}, sources=("ipinfo",))
        monkeypatch.setattr(cli, "_probe", lambda: probe)
    monkeypatch.setattr(cli, "real_ip", lambda: "1.1.1.1")
    return CliRunner().invoke(cli.main, ["status", "--json"], catch_exceptions=False)


def test_status_json_schema_running(monkeypatch):
    result = invoke_status(monkeypatch)
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert set(doc) == {
        "instance",
        "container_name",
        "image",
        "state",
        "selection",
        "drift",
        "control_server",
        "exit_ip",
        "leak",
        "verified",
        "last_error",
    }
    assert doc["instance"] == "epoxy"
    assert doc["container_name"] == "epoxy"
    assert doc["image"] == "qmcgaw/gluetun:latest"
    assert doc["state"] == "running"
    assert doc["selection"]["provider"] == "surfshark"
    assert doc["drift"] is False  # runtime matches baked env selection
    assert doc["control_server"] == {"port": 8000, "enabled": True}
    assert doc["exit_ip"] == {"ip": "9.9.9.9", "country": "Germany"}
    assert doc["leak"] is False
    assert doc["verified"] is True
    assert doc["last_error"] is None


def test_status_json_exit_1_on_leak(monkeypatch):
    result = invoke_status(monkeypatch, leak=True)
    assert result.exit_code == 1
    doc = json.loads(result.output)
    assert doc["leak"] is True
    assert doc["exit_ip"] == {"ip": "1.1.1.1", "country": "Germany"}


def test_status_json_absent_container(monkeypatch):
    result = invoke_status(monkeypatch, state="absent")
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["state"] == "absent"
    assert doc["image"] is None
    assert doc["selection"] is None
    assert doc["control_server"] == {"port": 8000, "enabled": False}
    assert doc["exit_ip"] is None
    assert doc["leak"] is False


def test_status_json_control_server_down(monkeypatch):
    """Unreachable control server while the container runs -> exit 1 (M9)."""
    result = invoke_status(monkeypatch, control_error="boom")
    assert result.exit_code == 1
    doc = json.loads(result.output)
    assert doc["selection"] is None
    assert doc["control_server"] == {"port": 8000, "enabled": False}
    assert doc["last_error"] == "control server unreachable (HTTP 500): boom"


def test_status_json_drift_when_hot_swapped(monkeypatch):
    result = invoke_status(
        monkeypatch,
        baked_provider="protonvpn",
        runtime=_settings(provider="surfshark", country="Japan"),
    )
    doc = json.loads(result.output)
    assert doc["drift"] is True
    assert doc["selection"]["country"] == "Japan"


def test_status_json_deterministic_single_line(monkeypatch):
    result = invoke_status(monkeypatch)
    assert result.output.strip().count("\n") == 0


def test_status_json_probe_totally_failed_is_not_leak(monkeypatch):
    """All echo providers down is probe health, not a leak (C8): reported in
    ``last_error`` with ``leak: false`` and exit 0."""
    result = invoke_status(monkeypatch, probe_fail=True)
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["leak"] is False
    assert doc["verified"] is False
    assert doc["exit_ip"] is None
    assert doc["last_error"] == "could not determine the exit IP (all echo services failed)"


def test_status_json_uses_published_port_without_registry(monkeypatch):
    """A registry-less (imported) running container is targeted via its
    published control port, not a blind port 8000."""
    monkeypatch.setattr("epoxy.discovery.container_status", lambda n=None: "running")
    monkeypatch.setattr(
        cli,
        "container_env",
        lambda: {
            "VPN_SERVICE_PROVIDER": "surfshark",
            "VPN_TYPE": "wireguard",
            "VPN_COUNTRY": "Germany",
        },
    )
    monkeypatch.setattr(cli, "container_image", lambda: "qmcgaw/gluetun:latest")
    monkeypatch.setattr(cli, "container_control_port", lambda name=None: 8123)
    monkeypatch.setattr(control, "get_settings", lambda: _settings(country="Germany"))
    probe = ipinfo._Probe({"ip": "9.9.9.9", "country": "DE"}, sources=("ipinfo",))
    monkeypatch.setattr(cli, "_probe", lambda: probe)
    monkeypatch.setattr(cli, "real_ip", lambda: "1.1.1.1")
    result = CliRunner().invoke(cli.main, ["status", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["control_server"] == {"port": 8123, "enabled": True}


def test_status_json_unknown_country_is_null_not_empty(monkeypatch):
    """valid echo-only IP with no geo info reports null country, not ''."""
    result = invoke_status(monkeypatch, probe_geo=False)
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["exit_ip"] == {"ip": "9.9.9.9", "country": None}
    assert doc["leak"] is False
