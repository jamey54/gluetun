"""Modal screens for the interactive UI.

Screens are deliberately thin: they collect input and return dataclasses from
``epoxy.tui.backend``; the dashboard performs the operation and refreshes. The
only screens that run work themselves are the ones whose whole purpose is to
show progress (an operation, a benchmark, or a live log), and those marshal all
UI updates back through ``App.call_from_thread``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import ClassVar, Generic, TypeVar

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    OptionList,
    ProgressBar,
    RichLog,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from epoxy.bench import BenchReport
from epoxy.config import DEFAULT_SCAN_SIZE_MB, DEFAULT_SIZE_MB, DEFAULT_TEST_CONCURRENCY
from epoxy.providers import PROVIDERS
from epoxy.tui.backend import (
    ActionResult,
    Backend,
    BenchOutcome,
    BenchRequest,
    ConnectRequest,
    CredentialChoice,
    Reporter,
    RotateRequest,
    ServerOption,
    StartRequest,
    describe_exception,
)
from epoxy.tui.state import (
    bench_rows,
    credential_markup,
    parse_optional_int,
    parse_publish_specs,
)

PROVIDER_OPTIONS = [(provider, provider) for provider in sorted(PROVIDERS)]
PROTOCOL_OPTIONS = [
    (protocol, protocol)
    for protocol in sorted({protocol for configs in PROVIDERS.values() for protocol in configs})
]


def _blank(value: object) -> bool:
    return value is Select.BLANK or value is Select.NULL


def _selected(select: Select[str]) -> str | None:
    value = select.value
    if _blank(value):
        return None
    return str(value)


def _text(value: str) -> str | None:
    stripped = value.strip()
    return stripped or None


def _select_default(value: str | None, options: list[tuple[str, str]]) -> str | bool:
    if value and any(value == option for _, option in options):
        return value
    return Select.BLANK


ResultT = TypeVar("ResultT")
BindingType = Binding | tuple[str, str] | tuple[str, str, str]


class _Modal(ModalScreen[ResultT], Generic[ResultT]):
    """Shared escape binding; subclasses override ``action_close``."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Close")]

    def action_close(self) -> None:
        self.dismiss(None)


class OperationScreen(_Modal[ActionResult | None]):
    """Run one blocking backend call in a thread, narrating its progress."""

    def __init__(
        self,
        title: str,
        operation: Callable[[Reporter], ActionResult],
    ) -> None:
        super().__init__()
        self.operation_title = title
        self.operation = operation
        self.finished = False
        self.result: ActionResult | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal"):
            yield Static(self.operation_title, classes="modal-title")
            yield RichLog(id="op-log", markup=True, wrap=True, auto_scroll=True)
            yield ProgressBar(total=100, show_eta=False, id="op-progress")
            with Horizontal(classes="buttons"):
                yield Button("Close", id="op-close", variant="primary", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#op-progress", ProgressBar).display = False
        self.run_worker(self._run, thread=True, exclusive=True)

    def _run(self) -> None:
        try:
            result = self.operation(self)
        except BaseException as exc:  # a failed operation must still close cleanly
            result = ActionResult(False, describe_exception(exc))
        self.app.call_from_thread(self._finish, result)

    def say(self, message: str) -> None:
        self.app.call_from_thread(self._write, message)

    def progress(self, done: int, total: int) -> None:
        self.app.call_from_thread(self._show_progress, done, total)

    def _write(self, message: str) -> None:
        self.query_one("#op-log", RichLog).write(escape(message))

    def _write_markup(self, markup: str) -> None:
        self.query_one("#op-log", RichLog).write(markup)

    def _show_progress(self, done: int, total: int) -> None:
        bar = self.query_one("#op-progress", ProgressBar)
        bar.display = True
        bar.update(total=max(total, 1), progress=min(done, max(total, 1)))

    def _finish(self, result: ActionResult) -> None:
        self.finished = True
        self.result = result
        self.query_one("#op-progress", ProgressBar).display = False
        colour = "green" if result.ok else "red"
        self._write_markup(f"[{colour}]{escape(result.message)}[/{colour}]")
        if result.detail:
            self._write(result.detail)
        if result.ok:
            self.dismiss(result)
            return
        close = self.query_one("#op-close", Button)
        close.disabled = False
        close.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "op-close":
            self.action_close()

    def action_close(self) -> None:
        if not self.finished:
            self.notify("Still running; please wait.", severity="warning")
            return
        self.dismiss(self.result)


class ConfirmScreen(_Modal[bool]):
    """Ask before a destructive action."""

    def __init__(
        self,
        message: str,
        *,
        title: str = "Confirm",
        confirm_label: str = "Confirm",
        danger: bool = False,
    ) -> None:
        super().__init__()
        self.message = message
        self.confirm_title = title
        self.confirm_label = confirm_label
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal small"):
            yield Static(self.confirm_title, classes="modal-title")
            yield Static(self.message, markup=False)
            with Horizontal(classes="buttons"):
                yield Button(
                    self.confirm_label,
                    id="confirm",
                    variant="error" if self.danger else "primary",
                )
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class CreateScreen(_Modal[StartRequest | None]):
    """Collect a ``StartRequest`` for a new (or recreated) instance."""

    def __init__(
        self,
        credentials: list[CredentialChoice],
        *,
        default_name: str = "",
        default_provider: str | None = None,
        default_protocol: str | None = None,
        default_country: str | None = None,
        default_city: str | None = None,
    ) -> None:
        super().__init__()
        self.credentials = credentials
        self.default_name = default_name
        self.default_provider = default_provider
        self.default_protocol = default_protocol
        self.default_country = default_country
        self.default_city = default_city

    def compose(self) -> ComposeResult:
        ready = [choice for choice in self.credentials if choice.ready]
        hint = (
            f"{len(ready)} credentialed provider/protocol pair(s)."
            if ready
            else "No complete credentials found — set them in .env first."
        )
        with VerticalScroll(classes="modal"):
            yield Static("New instance", classes="modal-title")
            yield Static(hint, classes="hint")
            yield Label("Name")
            yield Input(value=self.default_name, placeholder="epoxy", id="name")
            yield Label("Provider")
            yield Select(
                PROVIDER_OPTIONS,
                prompt="(keep current / choose)",
                value=_select_default(self.default_provider, PROVIDER_OPTIONS),
                id="provider",
            )
            yield Label("Protocol")
            yield Select(
                PROTOCOL_OPTIONS,
                prompt="(keep current / choose)",
                value=_select_default(self.default_protocol, PROTOCOL_OPTIONS),
                id="protocol",
            )
            yield Label("Country (optional)")
            yield Input(
                value=self.default_country or "", placeholder="e.g. Netherlands", id="country"
            )
            yield Label("City (optional)")
            yield Input(value=self.default_city or "", placeholder="e.g. Amsterdam", id="city")
            yield Label("Control port (optional)")
            yield Input(placeholder="auto", id="ctl-port")
            yield Label("Forwarded ports (optional)")
            yield Input(placeholder="8080:80, 5353:53/udp", id="publish")
            yield Checkbox("Append to existing forwarded ports", id="append-publish")
            yield Checkbox("Clear all forwarded ports", id="no-publish")
            yield Label("Env file (optional)")
            yield Input(placeholder=".env", id="env-file")
            yield Checkbox("Pull the latest image", id="pull")
            yield Checkbox("Recreate the container", id="recreate")
            yield Checkbox("Run a speed test after connecting", id="speedtest")
            with Horizontal(classes="buttons"):
                yield Button("Create", id="create", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "create":
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.notify("Name is required.", severity="error")
            return
        try:
            ctl_port = parse_optional_int(self.query_one("#ctl-port", Input).value, "Control port")
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        specs = parse_publish_specs(self.query_one("#publish", Input).value)
        append = self.query_one("#append-publish", Checkbox).value
        no_publish = self.query_one("#no-publish", Checkbox).value
        if append and no_publish:
            self.notify(
                "Choose either append or clear forwarded ports, not both.",
                severity="error",
            )
            return
        if append and not specs:
            self.notify("Append needs at least one port spec.", severity="error")
            return
        if no_publish and specs:
            self.notify(
                "Remove the port specs before clearing forwarded ports.",
                severity="error",
            )
            return
        request = StartRequest(
            name=name,
            provider=_selected(self.query_one("#provider", Select)),
            protocol=_selected(self.query_one("#protocol", Select)),
            country=_text(self.query_one("#country", Input).value),
            city=_text(self.query_one("#city", Input).value),
            env_file=_text(self.query_one("#env-file", Input).value),
            ctl_port=ctl_port,
            publish_specs=() if (append or no_publish) else specs,
            append_publish_specs=specs if append else (),
            no_publish=no_publish,
            pull=self.query_one("#pull", Checkbox).value,
            recreate=self.query_one("#recreate", Checkbox).value,
            speedtest=self.query_one("#speedtest", Checkbox).value,
        )
        self.dismiss(request)


class ConnectScreen(_Modal[ConnectRequest | None]):
    """Collect a ``ConnectRequest`` (hot-swap without restarting)."""

    def __init__(
        self,
        *,
        provider: str | None = None,
        protocol: str | None = None,
        country: str | None = None,
        city: str | None = None,
    ) -> None:
        super().__init__()
        self.default_provider = provider
        self.default_protocol = protocol
        self.default_country = country
        self.default_city = city

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal"):
            yield Static("Connect", classes="modal-title")
            yield Label("Provider")
            yield Select(
                PROVIDER_OPTIONS,
                prompt="(keep current)",
                value=_select_default(self.default_provider, PROVIDER_OPTIONS),
                id="provider",
            )
            yield Label("Protocol")
            yield Select(
                PROTOCOL_OPTIONS,
                prompt="(keep current)",
                value=_select_default(self.default_protocol, PROTOCOL_OPTIONS),
                id="protocol",
            )
            yield Label("Country")
            yield Input(value=self.default_country or "", placeholder="keep current", id="country")
            yield Label("City")
            yield Input(value=self.default_city or "", placeholder="optional", id="city")
            yield Checkbox("Run a speed test after connecting", value=True, id="speedtest")
            with Horizontal(classes="buttons"):
                yield Button("Connect", id="connect", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "connect":
            self._submit()

    def _submit(self) -> None:
        self.dismiss(
            ConnectRequest(
                name="",
                provider=_selected(self.query_one("#provider", Select)),
                protocol=_selected(self.query_one("#protocol", Select)),
                country=_text(self.query_one("#country", Input).value),
                city=_text(self.query_one("#city", Input).value),
                speedtest=self.query_one("#speedtest", Checkbox).value,
            )
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()


class RotateScreen(_Modal[RotateRequest | None]):
    """Collect rotation options; the dashboard applies them to every target."""

    def __init__(
        self,
        name: str,
        *,
        count: int = 1,
        country: str | None = None,
    ) -> None:
        super().__init__()
        self.instance_name = name
        self.count = count
        self.default_country = country

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal small"):
            yield Static(
                "Rotate exit IP" + (f" ({self.count} instances)" if self.count > 1 else ""),
                classes="modal-title",
            )
            yield Label("Country (optional)")
            yield Input(value=self.default_country or "", placeholder="any country", id="country")
            yield Checkbox("Save the winning location as instance config", id="save")
            yield Checkbox("Run a speed test after rotating", id="speedtest")
            with Horizontal(classes="buttons"):
                yield Button("Rotate", id="rotate", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "rotate":
            self._submit()

    def _submit(self) -> None:
        self.dismiss(
            RotateRequest(
                name=self.instance_name,
                country=_text(self.query_one("#country", Input).value),
                save=self.query_one("#save", Checkbox).value,
                speedtest=self.query_one("#speedtest", Checkbox).value,
            )
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()


class BenchScreen(_Modal[BenchRequest | None]):
    """Collect a ``BenchRequest`` before running the benchmark."""

    def __init__(
        self,
        name: str,
        *,
        provider: str | None = None,
        protocol: str | None = None,
        country: str | None = None,
    ) -> None:
        super().__init__()
        self.instance_name = name
        self.default_provider = provider
        self.default_protocol = protocol
        self.default_country = country

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal"):
            yield Static("Benchmark locations", classes="modal-title")
            yield Label("Provider")
            yield Select(
                PROVIDER_OPTIONS,
                prompt="(all credentialed)",
                value=_select_default(self.default_provider, PROVIDER_OPTIONS),
                id="provider",
            )
            yield Label("Protocol")
            yield Select(
                PROTOCOL_OPTIONS,
                prompt="(all credentialed)",
                value=_select_default(self.default_protocol, PROTOCOL_OPTIONS),
                id="protocol",
            )
            yield Label("Country (optional)")
            yield Input(value=self.default_country or "", placeholder="all countries", id="country")
            yield Label("Max candidates (0 = no cap)")
            yield Input(value="0", id="max-candidates")
            yield Label("Top locations")
            yield Input(value="12", id="top")
            yield Label("Screening size (MB)")
            yield Input(value=str(DEFAULT_SCAN_SIZE_MB), id="scan-size")
            yield Label("Final size (MB)")
            yield Input(value=str(DEFAULT_SIZE_MB), id="size")
            yield Label("Concurrency")
            yield Input(value=str(DEFAULT_TEST_CONCURRENCY), id="concurrency")
            yield Checkbox("Connect to the winner afterwards", id="connect-winner")
            with Horizontal(classes="buttons"):
                yield Button("Run benchmark", id="run", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "run":
            self._submit()

    def _submit(self) -> None:
        try:
            max_candidates = int(self.query_one("#max-candidates", Input).value.strip() or "0")
        except ValueError:
            self.notify("Max candidates must be a number.", severity="error")
            return
        if max_candidates < 0:
            self.notify("Max candidates cannot be negative.", severity="error")
            return
        try:
            top = parse_optional_int(self.query_one("#top", Input).value, "Top locations")
            scan = parse_optional_int(self.query_one("#scan-size", Input).value, "Screening size")
            size = parse_optional_int(self.query_one("#size", Input).value, "Final size")
            concurrency = parse_optional_int(
                self.query_one("#concurrency", Input).value, "Concurrency"
            )
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        if top is None or scan is None or size is None or concurrency is None:
            self.notify("All numeric fields are required.", severity="error")
            return
        self.dismiss(
            BenchRequest(
                name=self.instance_name,
                provider=_selected(self.query_one("#provider", Select)),
                protocol=_selected(self.query_one("#protocol", Select)),
                country=_text(self.query_one("#country", Input).value),
                max_candidates=max_candidates,
                top=top,
                scan_size=scan,
                size=size,
                concurrency=concurrency,
                connect_winner=self.query_one("#connect-winner", Checkbox).value,
            )
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()


class BenchRunScreen(_Modal[BenchOutcome | None]):
    """Run a benchmark and keep its report visible until dismissed."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Close")]

    def __init__(self, backend: Backend, request: BenchRequest) -> None:
        super().__init__()
        self.backend = backend
        self.request = request
        self.stop_event = threading.Event()
        self.finished = False
        self.outcome: BenchOutcome | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal wide"):
            yield Static(f"Benchmarking {self.request.name}", classes="modal-title")
            yield RichLog(id="bench-log", markup=True, wrap=True, max_lines=200)
            yield DataTable(id="bench-results", cursor_type="row")
            with Horizontal(classes="buttons"):
                yield Button("Stop", id="stop", variant="warning")
                yield Button("Close", id="close", variant="primary", disabled=True)

    def on_mount(self) -> None:
        table = self.query_one("#bench-results", DataTable)
        table.add_columns(
            "#", "Provider", "Protocol", "Location", "Latency", "Scan", "Final", "Notes"
        )
        self.run_worker(self._run, thread=True, exclusive=True)

    def _run(self) -> None:
        try:
            outcome = self.backend.bench(self.request, self, self.stop_event)
        except BaseException as exc:
            outcome = BenchOutcome(ActionResult(False, describe_exception(exc)))
        self.app.call_from_thread(self._finish, outcome)

    def say(self, message: str) -> None:
        self.app.call_from_thread(self._write, message)

    def progress(self, done: int, total: int) -> None:
        pass

    def _write(self, message: str) -> None:
        self.query_one("#bench-log", RichLog).write(escape(message))

    def _write_markup(self, markup: str) -> None:
        self.query_one("#bench-log", RichLog).write(markup)

    def _finish(self, outcome: BenchOutcome) -> None:
        self.finished = True
        self.outcome = outcome
        colour = "green" if outcome.result.ok else "red"
        self._write_markup(f"[{colour}]{escape(outcome.result.message)}[/{colour}]")
        if outcome.report is not None:
            self._fill(outcome.report)
        self.query_one("#stop", Button).disabled = True
        close = self.query_one("#close", Button)
        close.disabled = False
        close.focus()

    def _fill(self, report: BenchReport) -> None:
        table = self.query_one("#bench-results", DataTable)
        table.clear()
        for row in bench_rows(report):
            table.add_row(*row)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "stop":
            self.stop_event.set()
            event.button.disabled = True
            self._write("Stopping after the current candidate…")
        elif event.button.id == "close":
            self.action_close()

    def action_close(self) -> None:
        if not self.finished:
            self.stop_event.set()
            self.notify("Stopping…", severity="warning")
            return
        self.dismiss(self.outcome)


class LogsScreen(_Modal[None]):
    """Follow one instance's container logs."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Close")]

    def __init__(self, backend: Backend, name: str, *, tail: int = 200) -> None:
        super().__init__()
        self.backend = backend
        self.instance_name = name
        self.tail = tail
        self.stop_event = threading.Event()
        self.finished = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal wide"):
            yield Static(f"Logs — {self.instance_name}", classes="modal-title")
            yield RichLog(id="logs", markup=False, wrap=True, max_lines=2000, auto_scroll=True)
            with Horizontal(classes="buttons"):
                yield Button("Stop", id="stop", variant="warning")
                yield Button("Close", id="close", variant="primary", disabled=True)

    def on_mount(self) -> None:
        self.run_worker(self._run, thread=True, exclusive=True)

    def _run(self) -> None:
        try:
            self.backend.stream_logs(
                self.instance_name, self.tail, True, self._line, self.stop_event
            )
        except BaseException as exc:
            self.app.call_from_thread(self._write, f"Log stream failed: {describe_exception(exc)}")
        self.app.call_from_thread(self._finish)

    def _line(self, line: str) -> None:
        self.app.call_from_thread(self._write, line)

    def _write(self, line: str) -> None:
        self.query_one("#logs", RichLog).write(line)

    def _finish(self) -> None:
        self.finished = True
        self.query_one("#stop", Button).disabled = True
        close = self.query_one("#close", Button)
        close.disabled = False
        close.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "stop":
            self.stop_event.set()
            event.button.disabled = True
        elif event.button.id == "close":
            self.action_close()

    def action_close(self) -> None:
        if not self.finished:
            self.stop_event.set()
            self.notify("Stopping…", severity="warning")
            return
        self.dismiss(None)


class ServersScreen(_Modal[ServerOption | None]):
    """Read-only server browser; selecting a row offers a prefilled connect."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Close")]

    def __init__(self, servers: list[ServerOption]) -> None:
        super().__init__()
        self.servers = servers
        self.filtered = list(servers)

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal wide"):
            yield Static(f"Servers ({len(self.servers)})", classes="modal-title")
            yield Input(placeholder="Filter by provider, country, city, host…", id="server-filter")
            yield DataTable(id="server-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#server-table", DataTable)
        table.add_columns("Provider", "Protocol", "Country", "City", "Hostname")
        self._refresh()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "server-filter":
            return
        needle = event.value.strip().lower()
        self.filtered = [
            server
            for server in self.servers
            if not needle
            or needle
            in (
                f"{server.provider} {server.protocol} {server.country} "
                f"{server.city} {server.hostname}"
            ).lower()
        ]
        self._refresh()

    def _refresh(self) -> None:
        table = self.query_one("#server-table", DataTable)
        table.clear()
        for index, server in enumerate(self.filtered):
            table.add_row(
                server.provider,
                server.protocol,
                server.country,
                server.city or "-",
                server.hostname,
                key=str(index),
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        index = int(str(event.row_key.value))
        self.dismiss(self.filtered[index])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)


class CredentialsScreen(_Modal[None]):
    """Read-only credential readiness: env var names, never values."""

    def __init__(self, credentials: list[CredentialChoice], api_key: bool) -> None:
        super().__init__()
        self.credentials = credentials
        self.api_key = api_key

    def compose(self) -> ComposeResult:
        key_line = (
            "[green]✓[/green] HTTP_CONTROL_SERVER_API_KEY is set"
            if self.api_key
            else "[red]✕[/red] HTTP_CONTROL_SERVER_API_KEY is missing"
        )
        with VerticalScroll(classes="modal"):
            yield Static("Credentials", classes="modal-title")
            yield Static(key_line, markup=True)
            yield Static(credential_markup(self.credentials), markup=True)
            yield Static(
                "Values are never shown or edited here; update .env and restart epoxy.",
                classes="hint",
            )
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)


class HelpScreen(_Modal[None]):
    """Keymap and a short explanation of bulk actions."""

    KEYS: ClassVar[list[tuple[str, str]]] = [
        ("n", "Create a new instance"),
        ("s", "Start / reconnect selected instances"),
        ("x", "Stop selected instances"),
        ("d", "Remove selected instances"),
        ("c", "Connect (hot-swap) the highlighted instance"),
        ("o", "Rotate the exit IP of selected instances"),
        ("S", "Save the current location as instance config"),
        ("C", "Clear the instance's saved location"),
        ("l", "Follow logs for the highlighted instance"),
        ("t", "Speed-test selected instances"),
        ("b", "Benchmark locations for the highlighted instance"),
        ("u", "Trigger a server-list update"),
        ("D", "Toggle DNS-over-TLS"),
        ("v", "Browse servers"),
        ("e", "Show credential readiness"),
        ("space", "Toggle selection"),
        ("a", "Select all visible instances"),
        ("/", "Filter instances"),
        ("escape", "Clear filter or selection"),
        ("p", "Command palette"),
        ("r", "Refresh"),
        ("q", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        rows = "\n".join(
            f"[bold]{key:<8}[/bold] {escape(description)}" for key, description in self.KEYS
        )
        with VerticalScroll(classes="modal"):
            yield Static("Keys", classes="modal-title")
            yield Static(rows, markup=True)
            yield Static(
                "Actions apply to the multi-selection when there is one, otherwise "
                "to the highlighted row. Create, connect, logs and benchmark are "
                "single-instance actions.",
                classes="hint",
            )
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)


class PaletteScreen(_Modal[str | None]):
    """A small command palette over the dashboard's actions."""

    def __init__(self, actions: list[tuple[str, str]]) -> None:
        super().__init__()
        self.actions = actions
        self.filtered = list(actions)

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal small"):
            yield Static("Commands", classes="modal-title")
            yield Input(placeholder="Type to filter…", id="palette-filter")
            yield OptionList(id="palette-list")
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self._refresh()
        self.query_one("#palette-filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "palette-filter":
            return
        needle = event.value.strip().lower()
        self.filtered = [
            (action, description)
            for action, description in self.actions
            if not needle or needle in f"{action} {description}".lower()
        ]
        self._refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "palette-filter":
            self._choose()

    def _refresh(self) -> None:
        options = self.query_one("#palette-list", OptionList)
        options.clear_options()
        for action, description in self.filtered:
            options.add_option(Option(description, id=action))
        if self.filtered:
            options.highlighted = 0

    def _choose(self) -> None:
        options = self.query_one("#palette-list", OptionList)
        option = options.highlighted_option
        if option is not None and option.id is not None:
            self.dismiss(option.id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.dismiss(event.option.id)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
