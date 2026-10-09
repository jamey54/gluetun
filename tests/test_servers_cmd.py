"""Tests for `epoxy servers` (instance-independent server listing)."""

import json
import os
from typing import Any

import pytest

from epoxy import config
from tests.harness import run_bare_cli, run_cli

DATA = {
    "surfshark": [
        {"country": "Japan", "city": "Tokyo", "hostname": "jp1", "vpn": "wireguard"},
        {"country": "Germany", "city": "Berlin", "hostname": "de1", "vpn": "openvpn"},
    ],
    "protonvpn": [
        {"country": "Japan", "city": "Osaka", "hostname": "jp2", "vpn": "wireguard"},
    ],
}


@pytest.fixture()
def fetch(monkeypatch):
    """Stub the fetch layer; the command's narrowing and output stay real."""
    monkeypatch.setattr("epoxy.servers._read_cache", lambda: None)
    monkeypatch.setattr(
        "epoxy.servers._fetch_all_servers", lambda providers, image=None: dict(DATA)
    )
    monkeypatch.setattr("epoxy.servers._write_cache", lambda servers: None)
    # Hermetic env: the process environment only, never the developer's .env.
    monkeypatch.setattr("epoxy.commands.servers.build_env", lambda env_file: dict(os.environ))


@pytest.fixture()
def creds(monkeypatch):
    monkeypatch.setenv("SURFSHARK_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("SURFSHARK_OPENVPN_USER", "u")
    monkeypatch.setenv("SURFSHARK_OPENVPN_PASSWORD", "p")
    monkeypatch.setenv("PROTONVPN_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("PROTONVPN_WIREGUARD_ADDRESSES", "10.2.0.2/32")


def test_servers_json_lists_all(fetch, creds):
    result = run_cli(["servers", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    assert result.output.strip().count("\n") == 0
    assert json.loads(result.output) == {
        "servers": [
            {
                "provider": "protonvpn",
                "protocol": "wireguard",
                "country": "Japan",
                "city": "Osaka",
                "hostname": "jp2",
            },
            {
                "provider": "surfshark",
                "protocol": "openvpn",
                "country": "Germany",
                "city": "Berlin",
                "hostname": "de1",
            },
            {
                "provider": "surfshark",
                "protocol": "wireguard",
                "country": "Japan",
                "city": "Tokyo",
                "hostname": "jp1",
            },
        ]
    }


def test_servers_json_provider_filter(fetch, creds):
    result = run_cli(["servers", "--provider", "surfshark", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert [s["provider"] for s in doc["servers"]] == ["surfshark", "surfshark"]


def test_servers_json_protocol_filter(fetch, creds):
    result = run_cli(["servers", "--protocol", "openvpn", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert [(s["provider"], s["protocol"]) for s in doc["servers"]] == [("surfshark", "openvpn")]


def test_servers_unknown_provider_is_friendly(fetch, creds):
    result = run_cli(["servers", "--provider", "sufshark", "--json"])
    assert result.exit_code == 1
    assert "Unknown provider 'sufshark'" in result.output
    assert "Available: protonvpn, surfshark" in result.output
    assert "Traceback" not in result.output


def test_servers_filters_to_empty_is_friendly(fetch, creds):
    result = run_cli(["servers", "--provider", "protonvpn", "--protocol", "openvpn"])
    assert result.exit_code == 1
    assert "No matching servers for the given filters." in result.output


def test_servers_empty_fetch_is_friendly(monkeypatch, creds):
    monkeypatch.setattr("epoxy.servers._read_cache", lambda: None)
    monkeypatch.setattr("epoxy.servers._fetch_all_servers", lambda providers, image=None: {})
    monkeypatch.setattr("epoxy.servers._fetch_servers", lambda provider, image=None: [])
    monkeypatch.setattr("epoxy.servers._write_cache", lambda servers: None)
    monkeypatch.setattr("epoxy.commands.servers.build_env", lambda env_file: dict(os.environ))
    result = run_cli(["servers", "--json"])
    assert result.exit_code == 1
    assert "No servers found. Is Docker running?" in result.output


def test_servers_needs_no_instance(fetch, creds, monkeypatch):
    """The listing must never touch instance resolution (works pre-container)."""
    monkeypatch.setattr("epoxy.commands.servers.build_env", lambda env_file: dict(os.environ))
    monkeypatch.setattr(
        "epoxy.instance.current_instance",
        lambda: pytest.fail("must not resolve an instance"),
    )
    monkeypatch.setattr(
        "epoxy.instance.default_instance",
        lambda: pytest.fail("must not fall back to a default instance"),
    )
    monkeypatch.setattr(
        "epoxy.commands._common._choose_instance_name",
        lambda: pytest.fail("must not prompt"),
    )
    result = run_bare_cli(["servers", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    assert len(json.loads(result.output)["servers"]) == 3


def test_servers_human_prints_table(fetch, creds, monkeypatch):
    printed: list[Any] = []
    monkeypatch.setattr("epoxy.servers.print_servers_table", printed.append)
    result = run_cli(["servers"], catch_exceptions=False)
    assert result.exit_code == 0
    assert len(printed) == 1
    assert set(printed[0]) == {"surfshark", "protonvpn"}


def test_servers_reads_image_from_env(fetch, creds, monkeypatch):
    seen: dict[str, object] = {}

    def fake_fetch_all(providers, image=None):
        seen["image"] = image
        return dict(DATA)

    monkeypatch.setattr("epoxy.servers._fetch_all_servers", fake_fetch_all)
    result = run_cli(["servers", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    assert seen["image"] == config.DEFAULT_IMAGE

    monkeypatch.setenv("EPOXY_IMAGE", "qmcgaw/gluetun:v3.41.3")
    result = run_cli(["servers", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    assert seen["image"] == "qmcgaw/gluetun:v3.41.3"
