"""The epoxy dashboard: a master-detail instance table plus modal actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import ClassVar

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from epoxy.config import DEFAULT_SIZE_MB
from epoxy.tui.backend import (
    ActionResult,
    Backend,
    BenchRequest,
    ConnectRequest,
    InstanceStatus,
    InstanceSummary,
    RealBackend,
    Reporter,
    RotateRequest,
    ServerOption,
    StartRequest,
    describe_exception,
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
    OperationScreen,
    PaletteScreen,
    RotateScreen,
    ServersScreen,
)
from epoxy.tui.state import (
    detail_markup,
    filter_summaries,
    format_started,
    resolve_targets,
    state_style,
    state_symbol,
)

PALETTE_ACTIONS = [
    ("create", "Create a new instance"),
    ("start", "Start / reconnect selected instances"),
    ("stop", "Stop selected instances"),
    ("remove", "Remove selected instances"),
    ("connect", "Connect (hot-swap) the highlighted instance"),
    ("rotate", "Rotate the exit IP of selected instances"),
    ("save_current", "Save the current location as instance config"),
    ("clear_saved", "Clear the instance's saved location"),
    ("logs", "Follow logs for the highlighted instance"),
    ("speedtest", "Speed-test selected instances"),
    ("bench", "Benchmark locations for the highlighted instance"),
    ("update_servers", "Trigger a server-list update"),
    ("toggle_dns", "Toggle DNS-over-TLS"),
    ("servers", "Browse servers"),
    ("credentials", "Show credential readiness"),
    ("toggle_select", "Toggle the highlighted row's selection"),
    ("select_all", "Select all visible instances"),
    ("focus_filter", "Focus the filter box"),
    ("clear", "Clear filter or selection"),
    ("refresh", "Refresh instances"),
    ("help", "Show the keymap"),
    ("quit", "Quit epoxy"),
]


class DashboardScreen(Screen[None]):
    """Master-detail dashboard. Actions apply to the multi-selection, else the
    highlighted row; create/connect/logs/bench remain single-target."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("n", "create", "New"),
        Binding("s", "start", "Start"),
        Binding("x", "stop", "Stop"),
        Binding("d", "remove", "Remove"),
        Binding("c", "connect", "Connect"),
        Binding("o", "rotate", "Rotate"),
        Binding("S", "save_current", "Save"),
        Binding("C", "clear_saved", "Clear saved"),
        Binding("l", "logs", "Logs"),
        Binding("t", "speedtest", "Speed"),
        Binding("b", "bench", "Bench"),
        Binding("u", "update_servers", "Update"),
        Binding("D", "toggle_dns", "DNS"),
        Binding("v", "servers", "Servers"),
        Binding("e", "credentials", "Creds"),
        Binding("space", "toggle_select", "Select"),
        Binding("a", "select_all", "All"),
        Binding("escape", "clear", "Clear"),
        Binding("slash", "focus_filter", "Filter"),
        Binding("p", "palette", "Palette"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self, backend: Backend) -> None:
        super().__init__()
        self.backend = backend
        self.rows: list[InstanceSummary] = []
        self.visible_rows: list[InstanceSummary] = []
        self.selected_names: set[str] = set()
        self.cursor: str | None = None
        self.filter_text = ""
        self._detail_name: str | None = None

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Input(placeholder="Filter instances…  (/)", id="filter")
                yield DataTable(id="instances", cursor_type="row", zebra_stripes=True)
            yield Static("Loading…", id="detail", markup=True)
        yield RichLog(id="activity", markup=True, wrap=True, max_lines=200)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#instances", DataTable)
        table.add_columns("Sel", "State", "Name", "Provider", "Ctl", "Consumers", "Started")
        table.focus()
        self.set_interval(15, self._periodic_refresh)
        self._log("epoxy — ? for help, p for the command palette.")
        self.load_instances()

    # -- data loading ------------------------------------------------------

    @work(thread=True, exclusive=True, group="list")
    def load_instances(self) -> None:
        try:
            rows = self.backend.list_instances()
        except BaseException as exc:
            self.app.call_from_thread(self._load_failed, exc)
            return
        self.app.call_from_thread(self._apply_rows, rows)

    def _load_failed(self, exc: BaseException) -> None:
        message = describe_exception(exc)
        self._log(f"[red]Could not load instances: {escape(message)}[/red]")
        self.query_one("#detail", Static).update(f"[red]{escape(message)}[/red]")

    def _apply_rows(self, rows: list[InstanceSummary]) -> None:
        self.rows = rows
        self._detail_name = None
        self._render_table()

    @work(thread=True, exclusive=True, group="detail")
    def load_detail(self, name: str) -> None:
        try:
            status = self.backend.status(name)
        except BaseException as exc:
            self.app.call_from_thread(self._detail_failed, name, exc)
            return
        self.app.call_from_thread(self._show_detail, name, status)

    def _detail_failed(self, name: str, exc: BaseException) -> None:
        if self.cursor != name:
            return
        self.query_one("#detail", Static).update(f"[red]{escape(describe_exception(exc))}[/red]")

    def _show_detail(self, name: str, status: InstanceStatus) -> None:
        if self.cursor != name:
            return
        self.query_one("#detail", Static).update(detail_markup(status))

    def _periodic_refresh(self) -> None:
        if self.is_current:
            self.load_instances()

    # -- table rendering ---------------------------------------------------

    def _render_table(self) -> None:
        self.visible_rows = filter_summaries(self.rows, self.filter_text)
        names = {row.name for row in self.visible_rows}
        self.selected_names &= names
        table = self.query_one("#instances", DataTable)
        table.clear()
        for row in self.visible_rows:
            selected = row.name in self.selected_names
            state = Text(
                f"{state_symbol(row.state)} {row.state}",
                style=state_style(row.state),
            )
            table.add_row(
                Text("✓", style="bold green") if selected else "",
                state,
                row.name,
                row.selection_label,
                str(row.control_port) if row.control_port else "-",
                ", ".join(row.consumers) or "-",
                format_started(row.started_at),
                key=row.name,
            )
        if not self.visible_rows:
            self.cursor = None
            self._detail_name = None
            message = (
                "No instances match the filter."
                if self.filter_text
                else "No instances yet — press n to create one."
            )
            self.query_one("#detail", Static).update(message)
            return
        index = next(
            (i for i, row in enumerate(self.visible_rows) if row.name == self.cursor),
            0,
        )
        self.cursor = self.visible_rows[index].name
        table.move_cursor(row=index)
        if self._detail_name != self.cursor:
            self._detail_name = self.cursor
            self.query_one("#detail", Static).update(
                f"[bold]{escape(self.cursor)}[/bold]\n\n[dim]Loading details…[/dim]"
            )
            self.load_detail(self.cursor)

    # -- table events ------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key
        if key is None or key.value is None:
            return
        name = key.value
        if name == self.cursor:
            return
        self.cursor = name
        self._detail_name = name
        self.query_one("#detail", Static).update(
            f"[bold]{escape(name)}[/bold]\n\n[dim]Loading details…[/dim]"
        )
        self.load_detail(name)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._toggle(event.row_key.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "filter":
            return
        self.filter_text = event.value
        self._render_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            self.query_one("#instances", DataTable).focus()

    # -- helpers -----------------------------------------------------------

    def _log(self, message: str) -> None:
        self.query_one("#activity", RichLog).write(message)

    def _notify_result(self, result: ActionResult) -> None:
        colour = "green" if result.ok else "red"
        self._log(f"[{colour}]{'✓' if result.ok else '✕'} {escape(result.message)}[/{colour}]")
        self.notify(
            result.message,
            title="epoxy",
            severity="information" if result.ok else "error",
        )

    def _targets(self) -> list[str]:
        return resolve_targets(
            self.selected_names,
            self.cursor,
            [row.name for row in self.visible_rows],
        )

    def _single_target(self) -> str | None:
        targets = self._targets()
        if not targets:
            self.notify("Select or highlight an instance first.", severity="warning")
            return None
        if len(targets) > 1:
            self.notify(
                "This action is single-instance; clear the selection or pick one row.",
                severity="warning",
            )
            return None
        return targets[0]

    def _run_operation(
        self,
        title: str,
        operation: Callable[[Reporter], ActionResult],
    ) -> None:
        self.app.push_screen(OperationScreen(title, operation), self._operation_done)

    def _operation_done(self, result: ActionResult | None) -> None:
        if result is None:
            return
        self._notify_result(result)
        self.load_instances()

    def _toggle(self, name: str) -> None:
        if name in self.selected_names:
            self.selected_names.remove(name)
        else:
            self.selected_names.add(name)
        self._render_table()

    # -- actions -----------------------------------------------------------

    def action_quit(self) -> None:
        self.app.exit()

    def action_refresh(self) -> None:
        self.load_instances()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_clear(self) -> None:
        filter_input = self.query_one("#filter", Input)
        if filter_input.value:
            filter_input.value = ""
            self.filter_text = ""
            self._render_table()
        elif self.selected_names:
            self.selected_names.clear()
            self._render_table()

    def action_toggle_select(self) -> None:
        if self.cursor is not None:
            self._toggle(self.cursor)

    def action_select_all(self) -> None:
        self.selected_names = {row.name for row in self.visible_rows}
        self._render_table()

    def action_create(self) -> None:
        try:
            credentials = self.backend.credentials()
            api_key = self.backend.api_key_present()
        except BaseException as exc:
            self.notify(describe_exception(exc), severity="error")
            return
        if not api_key:
            self._log(
                "[yellow]HTTP_CONTROL_SERVER_API_KEY is missing; "
                "add it to .env before starting.[/yellow]"
            )
        default_name = "epoxy" if not any(row.name == "epoxy" for row in self.rows) else ""
        summary = next(
            (row for row in self.rows if row.name == self.cursor),
            self.rows[0] if self.rows else None,
        )
        selection = summary.selection if summary else None
        self.app.push_screen(
            CreateScreen(
                credentials,
                default_name=default_name,
                default_provider=selection.provider if selection else None,
                default_protocol=selection.protocol if selection else None,
                default_country=selection.country if selection else None,
                default_city=selection.city if selection else None,
            ),
            self._create_done,
        )

    def _create_done(self, request: StartRequest | None) -> None:
        if request is None:
            return
        self._run_operation(
            f"Create {request.name}",
            lambda reporter: self.backend.start(request, reporter),
        )

    def action_start(self) -> None:
        targets = self._targets()
        if not targets:
            self.notify("Select or highlight an instance first.", severity="warning")
            return
        self._run_operation(
            f"Start {len(targets)} instance(s)",
            lambda reporter: self._for_each(
                targets,
                lambda name: self.backend.start(StartRequest(name=name), reporter),
                reporter,
                "Started",
            ),
        )

    def action_stop(self) -> None:
        targets = self._targets()
        if not targets:
            self.notify("Select or highlight an instance first.", severity="warning")
            return
        self._run_operation(
            f"Stop {len(targets)} instance(s)",
            lambda reporter: self._for_each(
                targets,
                lambda name: self.backend.stop(name, reporter),
                reporter,
                "Stopped",
            ),
        )

    def action_remove(self) -> None:
        targets = self._targets()
        if not targets:
            self.notify("Select or highlight an instance first.", severity="warning")
            return
        consumers = {
            row.name: row.consumers for row in self.rows if row.name in targets and row.consumers
        }
        message = f"Remove {', '.join(targets)}?"
        if consumers:
            shared = "; ".join(
                f"{name} is shared by {', '.join(users)}"
                for name, users in sorted(consumers.items())
            )
            message += f"\n\nThis will force-remove shared instances: {shared}."
        force = bool(consumers)
        self.app.push_screen(
            ConfirmScreen(
                message,
                title="Remove instances",
                confirm_label="Remove",
                danger=True,
            ),
            lambda confirmed: self._remove_done(confirmed, targets, force),
        )

    def _remove_done(self, confirmed: bool, targets: list[str], force: bool) -> None:
        if not confirmed:
            return
        self._run_operation(
            f"Remove {len(targets)} instance(s)",
            lambda reporter: self._for_each(
                targets,
                lambda name: self.backend.remove(name, force, reporter),
                reporter,
                "Removed",
            ),
        )

    def action_connect(self) -> None:
        name = self._single_target()
        if name is None:
            return
        summary = next((row for row in self.rows if row.name == name), None)
        selection = summary.selection if summary else None
        self.app.push_screen(
            ConnectScreen(
                provider=selection.provider if selection else None,
                protocol=selection.protocol if selection else None,
                country=selection.country if selection else None,
                city=selection.city if selection else None,
            ),
            lambda request: self._connect_done(name, request),
        )

    def _connect_done(self, name: str, request: ConnectRequest | None) -> None:
        if request is None:
            return
        request = replace(request, name=name)
        self._run_operation(
            f"Connect {name}",
            lambda reporter: self.backend.connect(request, reporter),
        )

    def action_rotate(self) -> None:
        targets = self._targets()
        if not targets:
            self.notify("Select or highlight an instance first.", severity="warning")
            return
        summary = next((row for row in self.rows if row.name == targets[0]), None)
        country = summary.selection.country if summary and summary.selection else None
        self.app.push_screen(
            RotateScreen(targets[0], count=len(targets), country=country),
            lambda request: self._rotate_done(targets, request),
        )

    def _rotate_done(self, targets: list[str], request: RotateRequest | None) -> None:
        if request is None:
            return
        self._run_operation(
            f"Rotate {len(targets)} instance(s)",
            lambda reporter: self._for_each(
                targets,
                lambda name: self.backend.rotate(replace(request, name=name), reporter),
                reporter,
                "Rotated",
            ),
        )

    def action_save_current(self) -> None:
        name = self._single_target()
        if name is None:
            return
        self._run_operation(
            f"Save selection for {name}",
            lambda reporter: self.backend.save_current(name, reporter),
        )

    def action_clear_saved(self) -> None:
        name = self._single_target()
        if name is None:
            return
        self._run_operation(
            f"Clear saved selection for {name}",
            lambda reporter: self.backend.clear_saved(name, reporter),
        )

    def action_logs(self) -> None:
        name = self._single_target()
        if name is not None:
            self.app.push_screen(LogsScreen(self.backend, name))

    def action_speedtest(self) -> None:
        targets = self._targets()
        if not targets:
            self.notify("Select or highlight an instance first.", severity="warning")
            return
        self._run_operation(
            f"Speed test {len(targets)} instance(s)",
            lambda reporter: self._for_each(
                targets,
                lambda name: self.backend.speed_test(name, DEFAULT_SIZE_MB, reporter),
                reporter,
                "Tested",
            ),
        )

    def action_toggle_dns(self) -> None:
        targets = self._targets()
        if not targets:
            self.notify("Select or highlight an instance first.", severity="warning")
            return
        self._run_operation(
            f"Toggle DNS {len(targets)} instance(s)",
            lambda reporter: self._for_each(
                targets,
                lambda name: self.backend.toggle_dns(name, reporter),
                reporter,
                "Toggled DNS for",
            ),
        )

    def action_update_servers(self) -> None:
        targets = self._targets()
        if not targets:
            self.notify("Select or highlight an instance first.", severity="warning")
            return
        self._run_operation(
            f"Update servers {len(targets)} instance(s)",
            lambda reporter: self._for_each(
                targets,
                lambda name: self.backend.update_servers(name, reporter),
                reporter,
                "Updated servers for",
            ),
        )

    def action_bench(self) -> None:
        name = self._single_target()
        if name is None:
            return
        summary = next((row for row in self.rows if row.name == name), None)
        selection = summary.selection if summary else None
        self.app.push_screen(
            BenchScreen(
                name,
                provider=selection.provider if selection else None,
                protocol=selection.protocol if selection else None,
                country=selection.country if selection else None,
            ),
            self._bench_done,
        )

    def _bench_done(self, request: BenchRequest | None) -> None:
        if request is not None:
            self.app.push_screen(BenchRunScreen(self.backend, request))

    def action_servers(self) -> None:
        targets = self._targets()
        name = targets[0] if len(targets) == 1 else None
        self._fetch_servers(name)

    @work(thread=True, group="servers")
    def _fetch_servers(self, name: str | None) -> None:
        try:
            servers = self.backend.servers()
        except BaseException as exc:
            self.app.call_from_thread(self.notify, describe_exception(exc), severity="error")
            return
        self.app.call_from_thread(self._show_servers, servers, name)

    def _show_servers(self, servers: list[ServerOption], name: str | None) -> None:
        self.app.push_screen(
            ServersScreen(servers),
            lambda server: self._servers_done(server, name),
        )

    def _servers_done(self, server: ServerOption | None, name: str | None) -> None:
        if server is None:
            return
        if name is None:
            self.notify(
                "Highlight one instance to connect to the selected server.",
                severity="warning",
            )
            return
        self.app.push_screen(
            ConnectScreen(
                provider=server.provider,
                protocol=server.protocol,
                country=server.country,
                city=server.city or None,
            ),
            lambda request: self._connect_done(name, request),
        )

    def action_credentials(self) -> None:
        try:
            credentials = self.backend.credentials()
            api_key = self.backend.api_key_present()
        except BaseException as exc:
            self.notify(describe_exception(exc), severity="error")
            return
        self.app.push_screen(CredentialsScreen(credentials, api_key))

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_palette(self) -> None:
        self.app.push_screen(PaletteScreen(PALETTE_ACTIONS), self._palette_done)

    def _palette_done(self, action: str | None) -> None:
        if action is None:
            return
        handler = getattr(self, f"action_{action}", None)
        if callable(handler):
            handler()

    def _for_each(
        self,
        targets: list[str],
        action: Callable[[str], ActionResult],
        reporter: Reporter,
        verb: str,
    ) -> ActionResult:
        failures: list[str] = []
        for name in targets:
            reporter.say(f"{name}: working…")
            try:
                result = action(name)
            except BaseException as exc:
                failures.append(f"{name}: {describe_exception(exc)}")
                continue
            if not result.ok:
                failures.append(f"{name}: {result.message}")
            else:
                reporter.say(f"{name}: {result.message}")
        if failures:
            return ActionResult(
                False,
                f"{len(failures)} of {len(targets)} failed.",
                "\n".join(failures),
            )
        return ActionResult(True, f"{verb} {len(targets)} instance(s).")


class EpoxyApp(App[None]):
    """Interactive epoxy: dashboard by default, modals for every CLI action."""

    TITLE = "epoxy"
    SUB_TITLE = "VPN manager"
    CSS = """
    ModalScreen {
        align: center middle;
    }

    DashboardScreen {
        layout: vertical;
    }

    #main {
        height: 1fr;
    }

    #left {
        width: 3fr;
    }

    #filter {
        margin: 0 1;
    }

    #instances {
        height: 1fr;
    }

    #detail {
        width: 2fr;
        border: round $accent;
        padding: 0 1;
        overflow-y: auto;
    }

    #activity {
        height: 7;
        border-top: solid $accent;
        padding: 0 1;
    }

    .modal {
        width: 80%;
        max-width: 110;
        height: auto;
        max-height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    .modal.small {
        width: 60%;
    }

    .modal.wide {
        width: 90%;
        height: 85%;
    }

    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    .hint {
        color: $text-muted;
        margin: 1 0;
    }

    .buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
    }

    .buttons Button {
        margin-left: 1;
    }

    #op-log, #bench-log, #logs {
        height: 12;
        border: round $panel;
    }

    #bench-results, #server-table {
        height: 1fr;
    }

    #palette-list {
        height: 12;
    }
    """

    def __init__(self, backend: Backend | None = None) -> None:
        super().__init__()
        self.backend = backend or RealBackend()

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen(self.backend))


__all__ = ["PALETTE_ACTIONS", "DashboardScreen", "EpoxyApp"]
