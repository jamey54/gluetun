"""Tests for server fetching, caching, parsing, and display."""

import json
import time

import pytest

from vpn import config, servers
from vpn.providers import DEFAULT_PROTOCOL
from vpn.servers import (
    _parse_servers_output,
    _read_cache,
    _write_cache,
    listable_servers,
    parse_server_selection,
    print_servers_table,
    sorted_server_rows,
)
from vpn.textutil import strip_accents

SAMPLE_MD = """\
## Surfshark servers

| Country       | City     | Hostname        | VPN        |
| ------------- | -------- | --------------- | ---------- |
| United States | Boston   | `us-bos-001`    | wireguard  |
| Germany       | Cologne  | `de-cgn-ovpn-1` | openvpn    |
| Séoul         | Séoul    | `kr-sel-1`      |            |
"""


# ---------------------------------------------------------------------------
# strip_accents / sorting
# ---------------------------------------------------------------------------


def test_strip_accents():
    assert strip_accents("São Paulo") == "Sao Paulo"
    assert strip_accents("Zürich") == "Zurich"
    assert strip_accents("plain") == "plain"


def test_sorted_rows_accent_insensitive():
    rows = {
        "p": [
            {"country": "Österreich", "city": "Wien", "hostname": "a", "vpn": "wireguard"},
            {"country": "Oman", "city": "Muscat", "hostname": "b", "vpn": "openvpn"},
        ]
    }
    flat = sorted_server_rows(rows)
    assert [r[2] for r in flat] == ["Oman", "Österreich"]


def test_sorted_rows_sort_keys():
    by_provider = {
        "zeta": [{"country": "France", "city": "Paris", "hostname": "h", "vpn": "wireguard"}],
        "alpha": [
            {"country": "Brazil", "city": "São Paulo", "hostname": "h2", "vpn": "wireguard"},
            {"country": "Brazil", "city": "Rio", "hostname": "h1", "vpn": "openvpn"},
        ],
    }
    flat = sorted_server_rows(by_provider)
    assert [(r[0], r[3]) for r in flat] == [
        ("alpha", "Rio"),
        ("alpha", "São Paulo"),
        ("zeta", "Paris"),
    ]


# ---------------------------------------------------------------------------
# parse_server_selection
# ---------------------------------------------------------------------------


def test_parse_selection_full():
    assert parse_server_selection("[surfshark/wireguard] Netherlands - Amsterdam") == (
        "surfshark",
        "wireguard",
        "Netherlands",
        "Amsterdam",
    )


def test_parse_selection_provider_only_bracket():
    assert parse_server_selection("[protonvpn] United States") == (
        "protonvpn",
        None,
        "United States",
        None,
    )


def test_parse_selection_no_bracket_with_city():
    assert parse_server_selection("Germany - Berlin") == (None, None, "Germany", "Berlin")


def test_parse_selection_city_containing_dash():
    # only the first ' - ' separates country from city
    result = parse_server_selection("United States - Winston - Salem")
    assert result == (None, None, "United States", "Winston - Salem")


# ---------------------------------------------------------------------------
# gluetun markdown output parsing
# ---------------------------------------------------------------------------


def test_parse_servers_output_basic():
    parsed = _parse_servers_output(SAMPLE_MD.splitlines())
    assert {
        "country": "United States",
        "city": "Boston",
        "hostname": "us-bos-001",
        "vpn": "wireguard",
    } in parsed
    assert {
        "country": "Germany",
        "city": "Cologne",
        "hostname": "de-cgn-ovpn-1",
        "vpn": "openvpn",
    } in parsed


def test_parse_servers_output_defaults_vpn():
    parsed = _parse_servers_output(SAMPLE_MD.splitlines())
    row = next(r for r in parsed if r["country"] == "Séoul")
    assert row["vpn"] == DEFAULT_PROTOCOL


def test_parse_servers_output_no_header_fallback():
    # without a header, columns 1 and 2 (after the leading '|') are country/city
    lines = [
        "| France | Paris | x | y |",
    ]
    parsed = _parse_servers_output(lines)
    assert parsed == [{"country": "France", "city": "Paris", "hostname": "", "vpn": "wireguard"}]


def test_parse_servers_output_skips_separator_and_empty():
    lines = [
        "| Country | City | Hostname | VPN |",
        "| --- | --- | --- | --- |",
        "| Italy | Milan | `it-mil-1` | wireguard |",
        "| | | | |",
    ]
    assert len(_parse_servers_output(lines)) == 1


def test_parse_servers_output_empty():
    assert _parse_servers_output([]) == []
    assert _parse_servers_output(["no table here"]) == []


# ---------------------------------------------------------------------------
# cache round-trip
# ---------------------------------------------------------------------------


@pytest.fixture()
def cache_path(tmp_path, monkeypatch):
    path = tmp_path / "servers.json"
    monkeypatch.setattr(servers, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(servers, "CACHE_FILE", path)
    return path


def test_cache_roundtrip(cache_path):
    payload = {"surfshark": [{"country": "US", "city": "Boston", "hostname": "h", "vpn": "wg"}]}
    _write_cache(payload)
    assert _read_cache() == payload


def test_cache_stale_expired(cache_path):
    _write_cache({"s": []})
    data = json.loads(cache_path.read_text())
    data["ts"] = time.time() - config.CACHE_TTL - 1
    cache_path.write_text(json.dumps(data))
    assert _read_cache() is None


def test_cache_version_mismatch(cache_path):
    _write_cache({"s": []})
    data = json.loads(cache_path.read_text())
    data["v"] = servers.CACHE_VERSION + 1
    cache_path.write_text(json.dumps(data))
    assert _read_cache() is None


def test_cache_corrupt_json(cache_path):
    cache_path.write_text("{not json")
    assert _read_cache() is None


def test_cache_missing_file():
    assert _read_cache() is None


# ---------------------------------------------------------------------------
# get_servers / listable_servers
# ---------------------------------------------------------------------------


def test_get_servers_uses_cache(monkeypatch):
    cached = {"surfshark": [{"country": "US", "city": "X", "hostname": "", "vpn": "wireguard"}]}
    monkeypatch.setattr(servers, "_read_cache", lambda: cached)

    def fail_fetch(provider):  # pragma: no cover - must not be reached on cache hit
        raise AssertionError("network fetch should not run")

    monkeypatch.setattr(servers, "_fetch_servers", fail_fetch)
    assert servers.get_servers() == cached


def test_get_servers_does_not_cache_all_empty(monkeypatch):
    monkeypatch.setattr(servers, "_read_cache", lambda: None)
    monkeypatch.setattr(servers, "_fetch_servers", lambda provider: [])
    written = {}
    monkeypatch.setattr(servers, "_write_cache", lambda data: written.update(data))
    assert servers.get_servers() == {}
    assert written == {}


def test_get_servers_fetches_all_providers_in_parallel(cache_path, monkeypatch):
    monkeypatch.setattr(servers, "_read_cache", lambda: None)
    seen = []

    def fake_fetch(provider):
        seen.append(provider)
        return [{"country": provider.upper(), "city": "X", "hostname": "", "vpn": "wireguard"}]

    monkeypatch.setattr(servers, "_fetch_servers", fake_fetch)
    monkeypatch.setattr(
        servers,
        "get_active_providers",
        lambda: {("surfshark", "wireguard"), ("protonvpn", "wireguard")},
    )
    by_provider = servers.get_servers()
    assert sorted(seen) == ["protonvpn", "surfshark"]
    assert set(by_provider) == {"surfshark", "protonvpn"}
    assert by_provider["surfshark"][0]["country"] == "SURFSHARK"
    assert cache_path.exists()  # result cached


def test_listable_servers_filters_by_active_pair(monkeypatch):
    active = {("surfshark", "wireguard")}
    monkeypatch.setattr(servers, "get_active_providers", lambda: active)
    by_provider = {
        "surfshark": [
            {"country": "US", "city": "B", "hostname": "h", "vpn": "wireguard"},
            {"country": "DE", "city": "C", "hostname": "h", "vpn": "openvpn"},  # no creds
        ],
        "protonvpn": [  # entire provider inactive -> dropped
            {"country": "NL", "city": "A", "hostname": "h", "vpn": "wireguard"},
        ],
    }
    result = listable_servers(by_provider)
    assert set(result) == {"surfshark"}
    assert len(result["surfshark"]) == 1
    assert result["surfshark"][0]["country"] == "US"


# ---------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------


def test_print_servers_table_renders(capsys):
    by_provider = {
        "surfshark": [
            {"country": "US", "city": "Boston", "hostname": "us-1", "vpn": "wireguard"},
        ],
    }
    print_servers_table(by_provider)
    out = capsys.readouterr().out
    assert "Provider" in out and "surfshark" in out and "Boston" in out
    assert "1 server · 1 provider" in out
