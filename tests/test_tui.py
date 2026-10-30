"""The interactive UI, driven headlessly with a fake backend.

No Docker, network, or terminal is involved: ``EpoxyApp.run_test`` renders into
a headless terminal and ``FakeBackend`` records every operation.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable

from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, DataTable, Input, Select

from epoxy.apply import Selection
from epoxy.bench import BenchReport, BenchResult, Candidate
from epoxy.tui.app import DashboardScreen, EpoxyApp
from epoxy.tui.backend import (
    ActionResult,
    BenchOutcome,
    BenchRequest,
    ConnectRequest,
    CredentialChoice,
    InstanceStatus,
    InstanceSummary,
    Reporter,
    RotateRequest,
    ServerOption,
    StartRequest,
)
from epoxy.tui.screens import (
    BenchRunScreen,
    BenchScreen,
    ConfirmScreen,
    ConnectScreen,
    CreateScreen,
    CredentialsScreen,
    HelpScreen,
    LogsScreen,
    PaletteScreen,
    RotateScreen,
    ServersScreen,
    SpeedTestScreen,
)

ALPHA = InstanceSummary(
    name="alpha",
    state="running",
    selection=Selection("surfshark", "wireguard", "Netherlands", "Amsterdam"),
    control_port=8000,
    consumers=(),
    started_at="2026-01-01T00:00:00Z",
    forwarded_ports=(),
    saved_selection=None,
)
BETA = InstanceSummary(
    name="beta",
    state="stopped",
    selection=None,
    control_port=None,
    consumers=(),
    started_at=None,
    forwarded_ports=(),
    saved_selection=None,
)


class FakeBackend:
    """In-memory Backend: fast, deterministic, and free of side effects."""

    def __init__(self) -> None:
        self.rows = [ALPHA, BETA]
        self.calls: list[tuple[str, str]] = []
        self.logs_requested: list[str] = []
        self.bench_request: BenchRequest | None = None
        self.last_start: StartRequest | None = None
        self.speed_sizes: list[int] = []

    def docker_available(self) -> bool:
        return True

    def list_instances(self) -> list[InstanceSummary]:
        return list(self.rows)

    def status(self, name: str) -> InstanceStatus:
        row = next(row for row in self.rows if row.name == name)
        return InstanceStatus(
            name=name,
            state=row.state,
            image="qmcgaw/gluetun:latest",
            selection=row.selection,
            drift=False,
            control_port=row.control_port,
            control_enabled=row.state == "running",
            exit_ip="203.0.113.7" if row.state == "running" else None,
            exit_country="NL" if row.state == "running" else None,
            leak=False,
            verified=row.state == "running",
            last_error=None,
            forwarded_ports=row.forwarded_ports,
            consumers=row.consumers,
            saved_selection=row.saved_selection,
            tunnel="running" if row.state == "running" else None,
            dns="running" if row.state == "running" else None,
            port_forward=None,
        )

    def credentials(self) -> list[CredentialChoice]:
        return [
            CredentialChoice("surfshark", "wireguard", ()),
            CredentialChoice("protonvpn", "openvpn", ("PROTONVPN_OPENVPN_USER",)),
        ]

    def api_key_present(self) -> bool:
        return True

    def servers(self) -> list[ServerOption]:
        return [
            ServerOption("surfshark", "wireguard", "Netherlands", "Amsterdam", "nl-1"),
            ServerOption("surfshark", "wireguard", "Germany", "Berlin", "de-1"),
        ]

    def start(self, request: StartRequest, reporter: Reporter) -> ActionResult:
        self.calls.append(("start", request.name))
        self.last_start = request
        if not any(row.name == request.name for row in self.rows):
            self.rows.append(
                InstanceSummary(
                    name=request.name,
                    state="running",
                    selection=None,
                    control_port=8000 + len(self.rows),
                    consumers=(),
                    started_at="2026-01-02T00:00:00Z",
                    forwarded_ports=request.publish_specs,
                    saved_selection=None,
                )
            )
        return ActionResult(True, f"Started '{request.name}'.")

    def connect(self, request: ConnectRequest, reporter: Reporter) -> ActionResult:
        self.calls.append(("connect", request.name))
        return ActionResult(True, f"Connected {request.name}.")

    def rotate(self, request: RotateRequest, reporter: Reporter) -> ActionResult:
        self.calls.append(("rotate", request.name))
        return ActionResult(True, f"Rotated {request.name}.")

    def stop(self, name: str, reporter: Reporter) -> ActionResult:
        self.calls.append(("stop", name))
        return ActionResult(True, f"Stopped '{name}'.")

    def remove(self, name: str, force: bool, reporter: Reporter) -> ActionResult:
        self.calls.append(("remove", name))
        self.rows = [row for row in self.rows if row.name != name]
        return ActionResult(True, f"Removed '{name}'.")

    def toggle_dns(self, name: str, reporter: Reporter) -> ActionResult:
        self.calls.append(("dns", name))
        return ActionResult(True, f"DNS toggled for '{name}'.")

    def update_servers(self, name: str, reporter: Reporter) -> ActionResult:
        self.calls.append(("update", name))
        return ActionResult(True, f"Updated servers for '{name}'.")

    def speed_test(self, name: str, size: int, reporter: Reporter) -> ActionResult:
        self.calls.append(("speed", name))
        self.speed_sizes.append(size)
        return ActionResult(True, f"Speed test for '{name}'.")

    def bench(
        self,
        request: BenchRequest,
        reporter: Reporter,
        stop_event: threading.Event,
    ) -> BenchOutcome:
        self.bench_request = request
        candidate = Candidate("surfshark", "wireguard", "Netherlands", "Amsterdam")
        result = BenchResult(candidate, latency_s=0.05, scan_mbps=50.0, final_mbps=100.0)
        report = BenchReport(results=[result], winner=result, action="Benchmark complete.")
        return BenchOutcome(ActionResult(True, "Benchmark complete."), report)

    def stream_logs(
        self,
        name: str,
        tail: int,
        follow: bool,
        on_line: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None:
        self.logs_requested.append(name)
        on_line("alpha | starting")
        on_line("alpha | connected")

    def save_current(self, name: str, reporter: Reporter) -> ActionResult:
        self.calls.append(("save", name))
        return ActionResult(True, "Saved.")

    def clear_saved(self, name: str, reporter: Reporter) -> ActionResult:
        self.calls.append(("clear", name))
        return ActionResult(True, "Cleared.")


Scenario = Callable[[EpoxyApp, FakeBackend, Pilot[None]], Awaitable[None]]


def drive(scenario: Scenario) -> None:
    """Run one async scenario to completion."""

    async def main() -> None:
        backend = FakeBackend()
        app = EpoxyApp(backend)
        async with app.run_test(size=(140, 50)) as pilot:
            await settle(app, pilot)
            await scenario(app, backend, pilot)

    asyncio.run(main())


async def settle(app: EpoxyApp, pilot: Pilot[None]) -> None:
    """Let workers and queued messages finish."""
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def dashboard(app: EpoxyApp) -> DashboardScreen:
    assert isinstance(app.screen, DashboardScreen)
    return app.screen


def test_dashboard_lists_and_filters() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        screen = dashboard(app)
        table = screen.query_one("#instances", DataTable)
        assert table.row_count == 2
        assert screen.cursor == "alpha"

        filter_input = screen.query_one("#filter", Input)
        filter_input.value = "beta"
        await pilot.pause()
        assert table.row_count == 1
        assert screen.cursor == "beta"

        filter_input.value = ""
        await pilot.pause()
        assert table.row_count == 2

    drive(scenario)


def test_select_all_and_bulk_stop() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        screen = dashboard(app)
        await pilot.press("a")
        await pilot.pause()
        assert screen.selected_names == {"alpha", "beta"}
        await pilot.press("x")
        await settle(app, pilot)
        assert backend.calls == [("stop", "alpha"), ("stop", "beta")]
        assert isinstance(app.screen, DashboardScreen)

    drive(scenario)


def test_create_screen_starts_a_new_instance() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, CreateScreen)
        name = app.screen.query_one("#name", Input)
        name.value = "gamma"
        name.focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert ("start", "gamma") in backend.calls
        assert any(row.name == "gamma" for row in backend.rows)
        assert dashboard(app).query_one("#instances", DataTable).row_count == 3

    drive(scenario)


def test_connect_is_single_target_and_prefills_current_selection() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ConnectScreen)
        provider = app.screen.query_one("#provider", Select)
        assert provider.value == "surfshark"
        country = app.screen.query_one("#country", Input)
        country.value = "Germany"
        country.focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert backend.calls == [("connect", "alpha")]

    drive(scenario)


def test_logs_screen_streams_and_closes() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("l")
        await settle(app, pilot)
        assert isinstance(app.screen, LogsScreen)
        assert app.screen.finished
        assert backend.logs_requested == ["alpha"]
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)

    drive(scenario)


def test_bench_screen_runs_and_shows_results() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("b")
        await pilot.pause()
        assert isinstance(app.screen, BenchScreen)
        app.screen.query_one("#top", Input).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, BenchRunScreen)
        assert app.screen.finished
        assert backend.bench_request is not None
        assert app.screen.query_one("#bench-results", DataTable).row_count == 1
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)

    drive(scenario)


def test_remove_confirmation_and_force_for_shared_instances() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        shared = InstanceSummary(
            name="alpha",
            state="running",
            selection=ALPHA.selection,
            control_port=8000,
            consumers=("web",),
            started_at=ALPHA.started_at,
            forwarded_ports=(),
            saved_selection=None,
        )
        backend.rows = [shared, BETA]
        await pilot.press("r")
        await settle(app, pilot)
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#confirm")
        await settle(app, pilot)
        assert backend.calls == [("remove", "alpha")]
        assert not any(row.name == "alpha" for row in backend.rows)

    drive(scenario)


def test_palette_runs_an_action() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, PaletteScreen)
        palette_filter = app.screen.query_one("#palette-filter", Input)
        palette_filter.value = "credentials"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, CredentialsScreen)
        await pilot.press("escape")
        await pilot.pause()

    drive(scenario)


def test_servers_screen_prefills_connect() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("v")
        await settle(app, pilot)
        assert isinstance(app.screen, ServersScreen)
        table = app.screen.query_one("#server-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConnectScreen)
        assert app.screen.query_one("#provider", Select).value == "surfshark"
        await pilot.press("escape")
        await pilot.pause()

    drive(scenario)


def test_help_and_rotate_screens_open() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, RotateScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)

    drive(scenario)


def test_bulk_rotate_applies_options_to_every_target() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, RotateScreen)
        country = app.screen.query_one("#country", Input)
        country.value = "Germany"
        country.focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert backend.calls == [("rotate", "alpha"), ("rotate", "beta")]

    drive(scenario)


def test_speed_test_uses_the_chosen_size() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, SpeedTestScreen)
        size = app.screen.query_one("#size", Input)
        size.value = "5"
        size.focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert backend.calls == [("speed", "alpha")]
        assert backend.speed_sizes == [5]

    drive(scenario)


def test_save_and_clear_saved_selection_actions() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("S")
        await settle(app, pilot)
        await pilot.press("C")
        await settle(app, pilot)
        assert backend.calls == [("save", "alpha"), ("clear", "alpha")]

    drive(scenario)


def test_create_can_clear_forwarded_ports() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, CreateScreen)
        app.screen.query_one("#name", Input).value = "gamma"
        app.screen.query_one("#no-publish", Checkbox).value = True
        app.screen.query_one("#name", Input).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert backend.last_start is not None
        assert backend.last_start.no_publish is True
        assert backend.last_start.publish_specs == ()

    drive(scenario)


def test_bench_passes_max_candidates() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("b")
        await pilot.pause()
        assert isinstance(app.screen, BenchScreen)
        app.screen.query_one("#max-candidates", Input).value = "7"
        app.screen.query_one("#top", Input).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, BenchRunScreen)
        assert backend.bench_request is not None
        assert backend.bench_request.max_candidates == 7

    drive(scenario)


def test_create_screen_exposes_mouse_buttons() -> None:
    async def scenario(app: EpoxyApp, backend: FakeBackend, pilot: Pilot[None]) -> None:
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, CreateScreen)
        assert str(app.screen.query_one("#create", Button).label) == "Create"
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)

    drive(scenario)
