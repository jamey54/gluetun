"""Tests for the consolidated CLI surface: up/connect hot-swaps, status drift."""

from subprocess import CompletedProcess
from typing import Any

import pytest
from click.testing import CliRunner

from vpn import cli, config
from vpn.apply import Selection
from vpn.control import ControlError


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
def verified(monkeypatch):
    """Capture print_ip_status kwargs (stacks over the autouse stub)."""
    seen: list[dict[str, Any]] = []

    def record(**kwargs: Any) -> bool:
        seen.append(kwargs)
        return True

    monkeypatch.setattr("vpn.cli.print_ip_status", record)
    return seen


RUNNING = Selection("surfshark", "wireguard", "Germany")


def running(monkeypatch, sel: Selection | None = RUNNING):
    monkeypatch.setattr(cli, "container_running", lambda name=None: sel is not None)
    monkeypatch.setattr(cli, "effective_selection", lambda: sel)


@pytest.fixture()
def swaps(monkeypatch):
    seen: list[Selection] = []

    def record(sel: Selection) -> None:
        seen.append(sel)

    monkeypatch.setattr("vpn.cli.apply_location", record)
    return seen


@pytest.fixture()
def compose_calls(monkeypatch):
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def fake_compose(
        *args: str,
        env_overrides: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> CompletedProcess[str]:
        calls.append((args, env_overrides))
        return CompletedProcess((), 0)

    monkeypatch.setattr("vpn.cli.compose", fake_compose)
    return calls


def invoke(args: list[str]):
    return CliRunner().invoke(cli.main, args, catch_exceptions=False)


# ---------------------------------------------------------------------------
# up
# ---------------------------------------------------------------------------


def test_up_cold_start_bakes_provider_env(monkeypatch, compose_calls, swaps):
    running(monkeypatch, None)
    result = invoke(["up", "--provider", "surfshark"])
    assert result.exit_code == 0
    args, overrides = compose_calls[0]
    assert "--force-recreate" not in args
    assert overrides["VPN_SERVICE_PROVIDER"] == "surfshark"
    assert overrides["VPN_TYPE"] == "wireguard"
    assert "SERVER_COUNTRIES" not in overrides
    assert swaps == []  # no location request -> no swap


def test_up_cold_start_requires_provider(monkeypatch, compose_calls, swaps):
    running(monkeypatch, None)
    result = invoke(["up"])
    assert result.exit_code != 0
    assert "--provider" in result.output
    assert compose_calls == [] and swaps == []


def test_up_cold_start_with_country_swaps_after_start(monkeypatch, compose_calls, swaps, verified):
    running(monkeypatch, None)
    result = invoke(["up", "--provider", "protonvpn", "--country", "Japan"])
    assert result.exit_code == 0
    _, overrides = compose_calls[0]
    assert "SERVER_COUNTRIES" not in overrides  # location applied at runtime instead
    assert swaps == [Selection("protonvpn", "wireguard", "Japan")]
    assert verified[0]["expected_country"] == "Japan"


def test_up_running_no_flags_only_verifies(monkeypatch, compose_calls, swaps):
    running(monkeypatch)
    result = invoke(["up"])
    assert result.exit_code == 0
    assert compose_calls == [] and swaps == []


def test_up_running_no_flags_verifies_against_running_country(monkeypatch, verified):
    """A plain `vpn up` must still flag a geo mismatch (yellow), not blind green."""
    running(monkeypatch)
    result = invoke(["up"])
    assert result.exit_code == 0
    assert verified[0]["expected_country"] == "Germany"


def test_up_running_already_on_verifies_against_target_country(monkeypatch, verified):
    running(monkeypatch)
    result = invoke(["up", "--country", "Germany"])
    assert result.exit_code == 0
    assert verified[0]["expected_country"] == "Germany"


def test_up_running_country_hot_swaps(monkeypatch, compose_calls, swaps, verified):
    running(monkeypatch)
    result = invoke(["up", "--country", "Japan"])
    assert result.exit_code == 0
    assert compose_calls == []
    assert swaps == [Selection("surfshark", "wireguard", "Japan")]
    assert "Swapped to" in result.output


def test_up_running_same_target_short_circuits(monkeypatch, compose_calls, swaps):
    running(monkeypatch)
    result = invoke(["up", "--country", "Germany"])
    assert result.exit_code == 0
    assert swaps == []
    assert "Already on" in result.output


def test_up_provider_switch_resets_location(monkeypatch, compose_calls, swaps):
    running(monkeypatch)
    result = invoke(["up", "--provider", "protonvpn"])
    assert result.exit_code == 0
    assert swaps == [Selection("protonvpn", "wireguard", None)]


def test_up_city_only_keeps_country(monkeypatch, compose_calls, swaps):
    running(monkeypatch)
    result = invoke(["up", "--city", "Munich"])
    assert result.exit_code == 0
    assert swaps == [Selection("surfshark", "wireguard", "Germany", "Munich")]


def test_up_pull_pulls_image_and_recreates(monkeypatch, compose_calls, swaps):
    running(monkeypatch)
    pulls: list[tuple[str, ...]] = []

    def fake_pull(*args: str, timeout: float | None = None) -> CompletedProcess[str]:
        pulls.append(args)
        return CompletedProcess((), 0)

    monkeypatch.setattr("vpn.cli.run", fake_pull)
    result = invoke(["up", "--pull"])
    assert result.exit_code == 0
    assert any("pull" in c for c in pulls[0])
    args, _ = compose_calls[0]
    assert "--force-recreate" in args


def test_up_recreate_reverts_to_env_config(monkeypatch, compose_calls, swaps):
    running(monkeypatch)
    result = invoke(["up", "--recreate"])
    assert result.exit_code == 0
    args, _ = compose_calls[0]
    assert "--force-recreate" in args
    assert swaps == []


def test_up_recreate_same_country_still_swaps(monkeypatch, compose_calls, swaps, verified):
    """Recreate resets runtime state to env config, so an explicit request
    matching the pre-recreate selection must still be applied."""
    running(monkeypatch)  # on Germany before the recreate
    result = invoke(["up", "--recreate", "--country", "Germany"])
    assert result.exit_code == 0
    args, _ = compose_calls[0]
    assert "--force-recreate" in args
    assert swaps == [Selection("surfshark", "wireguard", "Germany")]
    assert verified[0]["expected_country"] == "Germany"


def test_up_city_without_any_country_fails_clearly(monkeypatch, compose_calls, swaps):
    running(monkeypatch, Selection("surfshark", "wireguard", None))
    result = invoke(["up", "--city", "Munich"])
    assert result.exit_code != 0
    assert "--country" in result.output
    assert swaps == [] and compose_calls == []


def test_up_running_unreachable_control_server_exits(monkeypatch, compose_calls, swaps):
    running(monkeypatch, None)
    monkeypatch.setattr(cli, "container_running", lambda name=None: True)
    result = invoke(["up", "--country", "Japan"])
    assert result.exit_code != 0
    assert "control server" in result.output
    assert swaps == [] and compose_calls == []


def test_up_recreate_works_with_unreachable_control_server(monkeypatch, compose_calls):
    """--recreate is the escape hatch: it must not block on the control server."""
    running(monkeypatch)
    monkeypatch.setattr(cli, "effective_selection", lambda: None)
    result = invoke(["up", "--provider", "surfshark", "--recreate"])
    assert result.exit_code == 0
    assert compose_calls[0][0] == ("up", "-d", "--force-recreate")


def test_up_explicit_protocol_requires_its_own_creds(monkeypatch, compose_calls, swaps):
    running(monkeypatch)
    result = invoke(["up", "--protocol", "openvpn"])
    assert result.exit_code != 0
    assert "Missing env vars" in result.output
    assert swaps == [] and compose_calls == []


def test_up_fails_closed_without_api_key(monkeypatch, compose_calls, swaps):
    running(monkeypatch, None)
    monkeypatch.delenv("HTTP_CONTROL_SERVER_API_KEY")
    monkeypatch.setattr("vpn.cli.env_lookup", lambda name: None)
    result = invoke(["up", "--provider", "surfshark"])
    assert result.exit_code != 0
    assert "HTTP_CONTROL_SERVER_API_KEY" in result.output
    assert compose_calls == [] and swaps == []


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def test_connect_requires_running_container(monkeypatch):
    running(monkeypatch, None)
    result = invoke(["connect", "--country", "Japan"])
    assert result.exit_code != 0
    assert "not running" in result.output


def test_connect_swap_excludes_previous_exit(monkeypatch, swaps, verified):
    running(monkeypatch)
    monkeypatch.setattr(cli, "current_exit_ip", lambda: "9.9.9.9")
    result = invoke(["connect", "--country", "Japan"])
    assert result.exit_code == 0
    assert swaps == [Selection("surfshark", "wireguard", "Japan")]
    assert verified[0]["expected_country"] == "Japan"
    assert verified[0]["exclude_ips"] == {"9.9.9.9"}


def test_connect_already_on_keeps_current_exit_valid(monkeypatch, swaps, verified):
    """No swap happened: the tunnel's current IP must not be excluded."""
    running(monkeypatch)
    monkeypatch.setattr(cli, "current_exit_ip", lambda: "1.1.1.1")
    result = invoke(["connect", "--country", "Germany"])
    assert result.exit_code == 0
    assert swaps == []
    assert "Already on" in result.output
    assert verified[0]["exclude_ips"] is None


def test_up_running_swap_excludes_previous_exit(monkeypatch, compose_calls, swaps, verified):
    running(monkeypatch)
    monkeypatch.setattr(cli, "current_exit_ip", lambda: "8.8.8.8")
    result = invoke(["up", "--country", "Japan"])
    assert result.exit_code == 0
    assert swaps == [Selection("surfshark", "wireguard", "Japan")]
    assert verified[0]["exclude_ips"] == {"8.8.8.8"}


def test_up_cold_start_has_no_previous_exit_to_exclude(monkeypatch, compose_calls, swaps, verified):
    calls: list[str] = []

    def probe() -> str | None:
        calls.append("probe")
        return "7.7.7.7"

    monkeypatch.setattr(cli, "current_exit_ip", probe)
    running(monkeypatch, None)
    result = invoke(["up", "--provider", "protonvpn", "--country", "Japan"])
    assert result.exit_code == 0
    assert calls == []  # nothing to exclude before a fresh start
    assert verified[0]["exclude_ips"] is None


def test_connect_picker_selection(monkeypatch, swaps):
    running(monkeypatch)
    _stub_server_rows(monkeypatch)
    monkeypatch.setattr(cli, "select_server", lambda rows: "[surfshark/wireguard] Japan - Tokyo")
    result = invoke(["connect"])
    assert result.exit_code == 0
    assert swaps == [Selection("surfshark", "wireguard", "Japan", "Tokyo")]


def test_connect_city_without_country_fails_clearly(monkeypatch, swaps):
    running(monkeypatch, Selection("surfshark", "wireguard", None))
    result = invoke(["connect", "--city", "Tokyo"])
    assert result.exit_code != 0
    assert "--country" in result.output
    assert swaps == []


def test_connect_cancelled_picker_exits(monkeypatch, swaps):
    running(monkeypatch)
    _stub_server_rows(monkeypatch)
    monkeypatch.setattr(cli, "select_server", lambda rows: None)
    result = invoke(["connect"])
    assert result.exit_code != 0
    assert "No selection." in result.output
    assert swaps == []


def test_connect_list_prints_table(monkeypatch, swaps):
    _stub_server_rows(monkeypatch)
    printed: list[Any] = []
    monkeypatch.setattr("vpn.cli.print_servers_table", printed.append)
    result = invoke(["connect", "--list"])
    assert result.exit_code == 0
    assert printed and printed[0]["surfshark"]
    assert swaps == []


def _stub_server_rows(monkeypatch) -> None:
    data = {"surfshark": [{"country": "Japan", "city": "", "hostname": "jp1", "vpn": "wireguard"}]}
    monkeypatch.setattr(cli, "get_servers", lambda: data)
    monkeypatch.setattr(cli, "listable_servers", lambda d: d)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_unknown_when_control_server_down(monkeypatch):
    monkeypatch.setattr(cli, "container_status", lambda: "running")
    monkeypatch.setattr(cli, "effective_selection", lambda: None)
    result = invoke(["status", "--no-speedtest"])
    assert result.exit_code == 0
    assert "unknown" in result.output


def test_status_missing_container(monkeypatch):
    monkeypatch.setattr(cli, "container_status", lambda: None)
    result = invoke(["status"])
    assert result.exit_code == 0
    assert "not found" in result.output


def test_status_shows_vpn_and_dns(monkeypatch):
    monkeypatch.setattr(cli, "container_status", lambda: "running")
    monkeypatch.setattr(cli, "effective_selection", lambda: RUNNING)
    monkeypatch.setattr("vpn.control.get_vpn_status", lambda: "running")
    monkeypatch.setattr("vpn.control.get_dns_status", lambda: "running")
    monkeypatch.setattr("vpn.control.get_port_forward", lambda: 5914)
    result = invoke(["status", "--no-speedtest"])
    assert result.exit_code == 0
    assert "Tunnel      running" in result.output
    assert "DNS         running" in result.output
    assert "Port fwd    5914" in result.output
    assert "Provider    surfshark" in result.output
    assert "Protocol    wireguard" in result.output


def test_status_flags_running_country_mismatch(monkeypatch, verified):
    """`vpn status` must pass the running country so a geo mismatch shows yellow."""
    monkeypatch.setattr(cli, "container_status", lambda: "running")
    monkeypatch.setattr(cli, "effective_selection", lambda: RUNNING)
    result = invoke(["status", "--no-speedtest"])
    assert result.exit_code == 0
    assert verified and verified[0]["expected_country"] == "Germany"


def test_status_does_not_probe_when_not_running(monkeypatch):
    """A stopped container must not stall the 15x2s verification retry loop."""
    calls: list[tuple[str, object]] = []

    def boom():
        raise ControlError(None, "no")

    monkeypatch.setattr(cli, "container_status", lambda: "exited")
    monkeypatch.setattr(cli, "effective_selection", lambda: None)
    monkeypatch.setattr("vpn.control.get_vpn_status", boom)
    monkeypatch.setattr(cli, "finish_connection", lambda **kw: (calls.append((kw, True)), True)[1])
    result = invoke(["status", "--no-speedtest"])
    assert result.exit_code == 0
    assert calls == []
    assert "exited" in result.output
    assert "Could not fetch public IP." not in result.output


def test_status_hides_dns_and_port_when_unreachable(monkeypatch):
    from vpn.control import ControlError

    def _control_error_noarg():
        raise ControlError(None, "no")

    monkeypatch.setattr(cli, "container_status", lambda: "running")
    monkeypatch.setattr(cli, "effective_selection", lambda: RUNNING)
    monkeypatch.setattr("vpn.control.get_vpn_status", _control_error_noarg)
    monkeypatch.setattr("vpn.control.get_dns_status", _control_error_noarg)
    monkeypatch.setattr("vpn.control.get_port_forward", _control_error_noarg)
    result = invoke(["status", "--no-speedtest"])
    assert result.exit_code == 0
    assert "VPN:" not in result.output
    assert "DNS:" not in result.output
    assert "Port fwd:" not in result.output


# ---------------------------------------------------------------------------
# down
# ---------------------------------------------------------------------------


def test_down_stops_vpn_before_compose(monkeypatch, compose_calls):
    stopped: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "vpn.control.set_vpn_status",
        lambda s, timeout=10: stopped.append((s, timeout)),
    )
    result = invoke(["down"])
    assert result.exit_code == 0
    assert stopped == [("stopped", config.DOWN_TIMEOUT_S)]
    assert compose_calls[0][0] == ("down",)
    assert "VPN stopped." in result.output


def test_down_succeeds_when_control_server_unreachable(monkeypatch, compose_calls):
    from vpn.control import ControlError

    def boom(s, timeout=10):
        raise ControlError(None, "no")

    monkeypatch.setattr("vpn.control.set_vpn_status", boom)
    result = invoke(["down"])
    assert result.exit_code == 0
    assert compose_calls[0][0] == ("down",)
    assert "VPN stopped." in result.output


def test_down_succeeds_when_control_server_times_out(monkeypatch, compose_calls):
    def boom(s, timeout=10):
        raise TimeoutError("timed out")

    monkeypatch.setattr("vpn.control.set_vpn_status", boom)
    result = invoke(["down"])
    assert result.exit_code == 0
    assert compose_calls[0][0] == ("down",)
    assert "VPN stopped." in result.output


# ---------------------------------------------------------------------------
# dns
# ---------------------------------------------------------------------------


def test_dns_no_action_shows_status(monkeypatch):
    monkeypatch.setattr("vpn.control.get_dns_status", lambda: "running")
    result = invoke(["dns"])
    assert result.exit_code == 0
    assert "DNS: running" in result.output


def test_dns_on_starts_resolver(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("vpn.control.set_dns_status", lambda s: calls.append(s))
    result = invoke(["dns", "on"])
    assert result.exit_code == 0
    assert calls == ["running"]
    assert "DNS running." in result.output


def test_dns_off_stops_resolver(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("vpn.control.set_dns_status", lambda s: calls.append(s))
    result = invoke(["dns", "off"])
    assert result.exit_code == 0
    assert calls == ["stopped"]
    assert "DNS stopped." in result.output


def test_dns_command_fails_without_control_server(monkeypatch):
    from vpn.control import ControlError

    monkeypatch.setattr(
        "vpn.control.get_dns_status",
        lambda: (_ for _ in ()).throw(ControlError(None, "no")),
    )
    result = invoke(["dns"])
    assert result.exit_code != 0
    assert "Cannot reach control server" in result.output


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_triggers_updater(monkeypatch):
    called = []
    monkeypatch.setattr("vpn.control.trigger_updater", lambda: called.append(True))
    result = invoke(["update"])
    assert result.exit_code == 0
    assert called == [True]
    assert "triggered" in result.output


def test_update_fails_without_control_server(monkeypatch):
    from vpn.control import ControlError

    def boom():
        raise ControlError(None, "no")

    monkeypatch.setattr("vpn.control.trigger_updater", boom)
    result = invoke(["update"])
    assert result.exit_code != 0
    assert "Cannot reach control server" in result.output
