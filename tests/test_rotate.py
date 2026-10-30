"""Tests for `epoxy rotate`: random location picks, retries, and persistence."""

import json
import random
import threading

import pytest

from epoxy import apply, config, docker
from epoxy.apply import Selection, Verification
from epoxy.commands import _common
from epoxy.instance import current_instance
from epoxy.rotate import folded_location, location_pool
from tests.harness import run_cli

ROWS = {
    "surfshark": [
        {"country": "Germany", "city": "Berlin", "hostname": "de-1", "vpn": "wireguard"},
        {"country": "Japan", "city": "Tokyo", "hostname": "jp-1", "vpn": "wireguard"},
        {"country": "Japan", "city": "Osaka", "hostname": "jp-2", "vpn": "wireguard"},
        {"country": "France", "city": "Paris", "hostname": "fr-1", "vpn": "openvpn"},
        {"country": "France", "city": "Paris", "hostname": "fr-2", "vpn": "wireguard"},
    ],
    "protonvpn": [
        {"country": "Japan", "city": "Tokyo", "hostname": "jp-p", "vpn": "wireguard"},
    ],
}

CURRENT = Selection("surfshark", "wireguard", "Germany", "Berlin")


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("SURFSHARK_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("PROTONVPN_WIREGUARD_PRIVATE_KEY", "k")
    monkeypatch.setenv("PROTONVPN_WIREGUARD_ADDRESSES", "10.2.0.2/32")
    monkeypatch.setenv("HTTP_CONTROL_SERVER_API_KEY", "test-key")
    monkeypatch.setattr("epoxy.servers.get_servers", lambda image=None: ROWS)
    monkeypatch.setattr(docker, "container_running", lambda name=None: True)
    monkeypatch.setattr(_common, "effective_selection", lambda: CURRENT)
    monkeypatch.setattr("epoxy.ipinfo.current_exit_ip", lambda: "10.0.0.1")
    monkeypatch.setattr("epoxy.commands.rotate._system_random", lambda: random.Random(0))


@pytest.fixture()
def applied(monkeypatch):
    """Record each applied selection with the instance it ran against."""
    seen: list[tuple[str, Selection]] = []

    def record(sel: Selection) -> None:
        seen.append((current_instance().name, sel))

    monkeypatch.setattr(apply, "apply_location", record)
    return seen


@pytest.fixture()
def verdicts(monkeypatch):
    """Queue verification outcomes; the default is a successful move."""
    queue: list[Verification] = []

    def fake_verify(sel: Selection, prev_ip: str | None = None) -> Verification:
        return queue.pop(0) if queue else Verification(True, ip="203.0.113.9")

    monkeypatch.setattr(apply, "verify", fake_verify)
    return queue


def seed_registry(name: str, selection: dict[str, str | None] | None = None) -> None:
    config.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    (config.INSTANCES_DIR / f"{name}.json").write_text(
        json.dumps(
            {
                "instance": name,
                "control_port": 8123,
                "env_file": None,
                "forwarded_ports": ["127.0.0.1:3000:3000"],
                "selection": selection,
            }
        )
    )


def read_selection(name: str) -> object:
    record = json.loads((config.INSTANCES_DIR / f"{name}.json").read_text())
    return record["selection"]


def test_rotate_picks_an_alternate_location(applied, verdicts):
    result = run_cli(["rotate"])
    assert result.exit_code == 0
    assert len(applied) == 1
    _, target = applied[0]
    assert target.provider == "surfshark" and target.protocol == "wireguard"
    assert (target.country, target.city) != (CURRENT.country, CURRENT.city)
    assert "Swapped to surfshark/wireguard" in result.output
    assert "203.0.113.9" in result.output


def test_rotate_country_narrows_the_pool(applied, verdicts):
    result = run_cli(["rotate", "--country", "japan"])
    assert result.exit_code == 0
    assert len(applied) == 1
    assert applied[0][1].country == "Japan"


def test_rotate_country_without_candidates_is_friendly(applied, verdicts):
    result = run_cli(["rotate", "--country", "Narnia"])
    assert result.exit_code == 1
    assert "No alternate surfshark/wireguard servers in Narnia." in result.output
    assert applied == []


def test_rotate_retries_when_the_exit_does_not_move(applied, verdicts):
    verdicts.extend(
        [
            Verification(False, reason="no reconnect"),
            Verification(True, ip="203.0.113.10"),
        ]
    )
    result = run_cli(["rotate"])
    assert result.exit_code == 0
    assert len(applied) == 2
    assert "no reconnect; trying another location." in result.output
    assert "203.0.113.10" in result.output


def test_rotate_gives_up_with_the_last_reason(applied, verdicts):
    verdicts.extend([Verification(False, reason="no reconnect") for _ in range(3)])
    result = run_cli(["rotate"])
    assert result.exit_code == 1
    assert "Could not move the exit IP after 3 tries (no reconnect)." in result.output


def test_rotate_all_gives_each_instance_a_distinct_location(monkeypatch, applied, verdicts):
    monkeypatch.setattr("epoxy.discovery.known_names", lambda: {"a", "b"})
    monkeypatch.setattr("epoxy.docker.container_control_port", lambda name=None: None)
    seed_registry("a")
    seed_registry("b")
    result = run_cli(["rotate", "--all"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "== a ==" in result.output and "== b ==" in result.output
    targets = [sel for _, sel in applied]
    assert len(targets) == 2
    assert len({(s.country, s.city) for s in targets}) == 2


def test_rotate_all_runs_instances_in_parallel(monkeypatch, verdicts):
    monkeypatch.setattr("epoxy.discovery.known_names", lambda: {"a", "b"})
    monkeypatch.setattr("epoxy.docker.container_control_port", lambda name=None: None)
    seed_registry("a")
    seed_registry("b")
    # A sequential run would leave the first waiter hanging until this times
    # out; only overlapping rotations can both pass the barrier.
    barrier = threading.Barrier(2, timeout=5)
    seen: list[str] = []
    guard = threading.Lock()

    def record(sel: Selection) -> None:
        with guard:
            seen.append(current_instance().name)
        barrier.wait()

    monkeypatch.setattr(apply, "apply_location", record)
    result = run_cli(["rotate", "--all"], catch_exceptions=False)
    assert result.exit_code == 0
    assert sorted(seen) == ["a", "b"]


def test_rotate_all_keeps_output_grouped_per_instance(monkeypatch, applied, verdicts):
    monkeypatch.setattr("epoxy.discovery.known_names", lambda: {"a", "b"})
    monkeypatch.setattr("epoxy.docker.container_control_port", lambda name=None: None)
    seed_registry("a")
    seed_registry("b")
    result = run_cli(["rotate", "--all"], catch_exceptions=False)
    assert result.exit_code == 0
    a_head = result.output.index("== a ==")
    b_head = result.output.index("== b ==")
    assert a_head < b_head
    assert result.output.count("Swapped to") == 2
    assert a_head < result.output.index("Swapped to") < b_head


def test_rotate_all_continues_past_a_stopped_instance(monkeypatch, applied, verdicts):
    monkeypatch.setattr("epoxy.discovery.known_names", lambda: {"a", "b"})
    monkeypatch.setattr("epoxy.docker.container_control_port", lambda name=None: None)
    monkeypatch.setattr(
        docker, "container_running", lambda name=None: current_instance().name != "a"
    )
    seed_registry("a")
    seed_registry("b")
    result = run_cli(["rotate", "--all"])
    assert result.exit_code == 1
    assert "a: Error: Container 'a' is not running." in result.output
    assert [name for name, _ in applied] == ["b"]


def test_rotate_save_persists_the_winning_location(applied, verdicts):
    seed_registry("epoxy")
    result = run_cli(["rotate", "--save"])
    assert result.exit_code == 0
    assert "Saved as instance config" in result.output
    _, target = applied[0]
    assert read_selection("epoxy") == {
        "provider": target.provider,
        "protocol": target.protocol,
        "country": target.country,
        "city": target.city,
    }
    record = json.loads((config.INSTANCES_DIR / "epoxy.json").read_text())
    assert record["forwarded_ports"] == ["127.0.0.1:3000:3000"]


def test_rotate_save_requires_a_registry_record(applied, verdicts):
    result = run_cli(["rotate", "--save"])
    assert result.exit_code == 1
    assert "no registry record" in result.output
    assert applied == []


def test_rotate_clear_drops_the_saved_location(applied, verdicts):
    seed_registry(
        "epoxy",
        {"provider": "surfshark", "protocol": "wireguard", "country": "Japan", "city": "Tokyo"},
    )
    result = run_cli(["rotate", "--clear"])
    assert result.exit_code == 0
    assert "Saved location cleared" in result.output
    assert read_selection("epoxy") is None
    assert applied == []


def test_rotate_clear_when_nothing_is_saved(applied, verdicts):
    seed_registry("epoxy")
    result = run_cli(["rotate", "--clear"])
    assert result.exit_code == 0
    assert "No saved location." in result.output


def test_rotate_rejects_conflicting_modes():
    for args in (
        ["--save", "--clear"],
        ["--clear", "--country", "Japan"],
        ["--clear", "--speedtest"],
    ):
        result = run_cli(["rotate", *args])
        assert result.exit_code == 2


def test_rotate_speedtest_is_opt_in(monkeypatch, applied, verdicts):
    calls: list[bool] = []
    monkeypatch.setattr(_common, "speed_test", lambda size=25, **_: calls.append(True))
    assert run_cli(["rotate"]).exit_code == 0
    assert calls == []
    assert run_cli(["rotate", "--speedtest"]).exit_code == 0
    assert calls == [True]


def test_rotate_without_servers_is_friendly(monkeypatch, applied, verdicts):
    monkeypatch.setattr("epoxy.servers.get_servers", lambda image=None: {})
    result = run_cli(["rotate"])
    assert result.exit_code == 1
    assert "No servers found. Is Docker running?" in result.output
    assert applied == []


def test_location_pool_filters_pair_country_and_duplicates():
    rows = [
        {"country": "Japan", "city": "Tokyo", "vpn": "wireguard"},
        {"country": "Japan", "city": "Tokyo", "vpn": "wireguard"},
        {"country": "Japan", "city": "Osaka", "vpn": "wireguard"},
        {"country": "Japan", "city": "Tokyo", "vpn": "openvpn"},
        {"country": "France", "city": "Paris", "vpn": "wireguard"},
        {"country": "Japan", "city": "", "vpn": "wireguard"},
    ]
    assert location_pool(rows, "surfshark", "wireguard", country="Japan") == [
        ("Japan", None),
        ("Japan", "Osaka"),
        ("Japan", "Tokyo"),
    ]
    assert location_pool(
        rows,
        "surfshark",
        "wireguard",
        exclude={folded_location("Japan", "Tokyo"), folded_location("Japan", "")},
    ) == [("France", "Paris"), ("Japan", "Osaka")]
