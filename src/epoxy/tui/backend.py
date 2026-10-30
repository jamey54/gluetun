"""Operations the interactive UI drives, kept apart from Textual widgets.

Every function here is synchronous and safe to call from a worker thread. The
real backend reuses the same low-level modules and command helpers as the CLI;
``FakeBackend`` in the tests implements the same protocol without Docker or a
network.
"""

from __future__ import annotations

import contextlib
import os
import random
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

import click

from epoxy import apply, control, discovery, docker, ipinfo, servers, speedtest
from epoxy.apply import Selection
from epoxy.bench import BenchReport, build_candidates, run_bench
from epoxy.commands import _common
from epoxy.commands.status import _status_doc
from epoxy.commands.up import _check_forward_collisions, _resolve_forwarded
from epoxy.config import (
    COMPOSE_TIMEOUT_S,
    CTL_PORT_ENV_VAR,
    DEFAULT_IMAGE,
    DEFAULT_SCAN_SIZE_MB,
    DEFAULT_SIZE_MB,
    IMAGE_ENV_VAR,
    PULL_TIMEOUT_S,
    ROTATE_ATTEMPTS,
)
from epoxy.control import ControlError
from epoxy.instance import (
    COMPOSE_SERVICE,
    Instance,
    SavedSelection,
    allocate_free_port,
    build_env,
    current_instance,
    delete_instance_state,
    ensure_compose_file,
    image_ref,
    instance_context,
    read_registry,
    sync_registry,
    update_saved_selection,
    write_registry,
)
from epoxy.providers import (
    PROVIDERS,
    get_active_providers_in,
    get_provider_env,
    resolve_provider,
)
from epoxy.rotate import folded_location, location_pool, shuffle_locations
from epoxy.textutil import fold


class Reporter(Protocol):
    """Where an operation narrates progress (a CLI echo, or a TUI log pane)."""

    def say(self, message: str) -> None: ...

    def progress(self, done: int, total: int) -> None: ...


class SilentReporter:
    """Reporter for tests and for operations that need no narration."""

    def say(self, message: str) -> None:
        pass

    def progress(self, done: int, total: int) -> None:
        pass


@dataclass(frozen=True)
class InstanceSummary:
    """Cheap list row: everything ``epoxy ls`` knows, plus saved config."""

    name: str
    state: str
    selection: Selection | None
    control_port: int | None
    consumers: tuple[str, ...]
    started_at: str | None
    forwarded_ports: tuple[str, ...]
    saved_selection: SavedSelection | None

    @property
    def selection_label(self) -> str:
        if self.selection is None:
            return "-"
        loc = ", ".join(filter(None, [self.selection.city, self.selection.country]))
        base = f"{self.selection.provider}/{self.selection.protocol}"
        return f"{base} → {loc}" if loc else base


@dataclass(frozen=True)
class InstanceStatus:
    """Full detail view: the status document plus control-server extras."""

    name: str
    state: str
    image: str | None
    selection: Selection | None
    drift: bool
    control_port: int | None
    control_enabled: bool
    exit_ip: str | None
    exit_country: str | None
    leak: bool
    verified: bool
    last_error: str | None
    forwarded_ports: tuple[str, ...]
    consumers: tuple[str, ...]
    saved_selection: SavedSelection | None
    tunnel: str | None
    dns: str | None
    port_forward: int | None

    @property
    def selection_label(self) -> str:
        if self.selection is None:
            return "unknown"
        loc = ", ".join(filter(None, [self.selection.city, self.selection.country]))
        base = f"{self.selection.provider}/{self.selection.protocol}"
        return f"{base} → {loc}" if loc else base


@dataclass(frozen=True)
class CredentialChoice:
    """One provider/protocol pair and the env vars it still needs."""

    provider: str
    protocol: str
    missing: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.missing


@dataclass(frozen=True)
class ServerOption:
    provider: str
    protocol: str
    country: str
    city: str
    hostname: str

    @property
    def label(self) -> str:
        location = f"{self.country} / {self.city}" if self.city else self.country
        return f"{self.provider}/{self.protocol} · {location}"


@dataclass(frozen=True)
class StartRequest:
    name: str
    provider: str | None = None
    protocol: str | None = None
    country: str | None = None
    city: str | None = None
    env_file: str | None = None
    ctl_port: int | None = None
    publish_specs: tuple[str, ...] = ()
    append_publish_specs: tuple[str, ...] = ()
    no_publish: bool = False
    pull: bool = False
    recreate: bool = False
    speedtest: bool = False
    size: int = DEFAULT_SIZE_MB


@dataclass(frozen=True)
class ConnectRequest:
    name: str
    provider: str | None = None
    protocol: str | None = None
    country: str | None = None
    city: str | None = None
    speedtest: bool = False
    size: int = DEFAULT_SIZE_MB


@dataclass(frozen=True)
class RotateRequest:
    name: str
    country: str | None = None
    save: bool = False
    speedtest: bool = False
    size: int = DEFAULT_SIZE_MB


@dataclass(frozen=True)
class BenchRequest:
    name: str
    provider: str | None = None
    protocol: str | None = None
    country: str | None = None
    max_candidates: int = 0
    top: int = 12
    scan_size: int = DEFAULT_SCAN_SIZE_MB
    size: int = DEFAULT_SIZE_MB
    concurrency: int = 1
    connect_winner: bool = False


@dataclass(frozen=True)
class ActionResult:
    """The user-facing outcome of one operation."""

    ok: bool
    message: str
    detail: str = ""


@dataclass(frozen=True)
class BenchOutcome:
    result: ActionResult
    report: BenchReport | None = None


def describe_exception(exc: BaseException) -> str:
    """Turn command-layer exceptions into one friendly line (never a traceback)."""
    if isinstance(exc, click.ClickException):
        return exc.message
    if isinstance(exc, SystemExit):
        code = exc.code
        if isinstance(code, str) and code:
            return code
        if code in (None, 0):
            return ""
        return "Operation failed."
    text = str(exc).strip()
    return text or type(exc).__name__


def _selection_from_doc(doc: object) -> Selection | None:
    if not isinstance(doc, dict):
        return None
    provider = doc.get("provider")
    if not isinstance(provider, str) or not provider:
        return None
    protocol = doc.get("protocol")
    country = doc.get("country")
    city = doc.get("city")
    return Selection(
        provider,
        protocol if isinstance(protocol, str) else "",
        country if isinstance(country, str) and country else None,
        city if isinstance(city, str) and city else None,
    )


def _saved_selection(name: str) -> SavedSelection | None:
    record = read_registry(name)
    return SavedSelection.from_doc(record.get("selection")) if record else None


def _control_port(record: dict[str, object]) -> int | None:
    control_doc = record.get("control_server")
    if isinstance(control_doc, dict):
        port = control_doc.get("port")
        if isinstance(port, int):
            return port
    return None


def _summary(record: dict[str, object]) -> InstanceSummary:
    name = str(record["instance"])
    raw_forwards = record.get("forwarded_ports")
    forwards = (
        tuple(spec for spec in raw_forwards if isinstance(spec, str))
        if isinstance(raw_forwards, list)
        else ()
    )
    consumers = record.get("consumers")
    return InstanceSummary(
        name=name,
        state=str(record["state"]),
        selection=_selection_from_doc(record.get("selection")),
        control_port=_control_port(record),
        consumers=tuple(str(c) for c in consumers) if isinstance(consumers, list) else (),
        started_at=str(record["started_at"]) if record.get("started_at") else None,
        forwarded_ports=forwards,
        saved_selection=_saved_selection(name),
    )


class Backend(Protocol):
    """Everything the UI can ask epoxy to do."""

    def docker_available(self) -> bool: ...

    def list_instances(self) -> list[InstanceSummary]: ...

    def status(self, name: str) -> InstanceStatus: ...

    def credentials(self) -> list[CredentialChoice]: ...

    def api_key_present(self) -> bool: ...

    def servers(self) -> list[ServerOption]: ...

    def start(self, request: StartRequest, reporter: Reporter) -> ActionResult: ...

    def connect(self, request: ConnectRequest, reporter: Reporter) -> ActionResult: ...

    def rotate(self, request: RotateRequest, reporter: Reporter) -> ActionResult: ...

    def stop(self, name: str, reporter: Reporter) -> ActionResult: ...

    def remove(self, name: str, force: bool, reporter: Reporter) -> ActionResult: ...

    def toggle_dns(self, name: str, reporter: Reporter) -> ActionResult: ...

    def update_servers(self, name: str, reporter: Reporter) -> ActionResult: ...

    def speed_test(self, name: str, size: int, reporter: Reporter) -> ActionResult: ...

    def bench(
        self,
        request: BenchRequest,
        reporter: Reporter,
        stop_event: threading.Event,
    ) -> BenchOutcome: ...

    def stream_logs(
        self,
        name: str,
        tail: int,
        follow: bool,
        on_line: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None: ...

    def save_current(self, name: str, reporter: Reporter) -> ActionResult: ...

    def clear_saved(self, name: str, reporter: Reporter) -> ActionResult: ...


class RealBackend:
    """Backend backed by Docker, the control server, and the shared helpers."""

    # -- discovery ---------------------------------------------------------

    def docker_available(self) -> bool:
        result = docker.run(
            "docker",
            "version",
            "--format",
            "{{.Server.Version}}",
            capture=True,
            check=False,
            timeout=10,
        )
        return result.returncode == 0

    def list_instances(self) -> list[InstanceSummary]:
        return [_summary(record) for record in discovery.instance_records()]

    def status(self, name: str) -> InstanceStatus:
        inst = _common._resolve_for_command(name)
        with instance_context(inst):
            doc = _status_doc()
            state = str(doc["state"])
            control_doc = doc.get("control_server")
            control_enabled = (
                bool(control_doc.get("enabled")) if isinstance(control_doc, dict) else False
            )
            tunnel: str | None = None
            dns: str | None = None
            port_forward: int | None = None
            if state in ("running", "starting") and control_enabled:
                with contextlib.suppress(ControlError):
                    tunnel = control.get_tunnel_status()
                with contextlib.suppress(ControlError):
                    dns = control.get_dns_status()
                with contextlib.suppress(ControlError):
                    port_forward = control.get_port_forward()
            exit_doc = doc.get("exit_ip")
            exit_ip = None
            exit_country = None
            if isinstance(exit_doc, dict):
                raw_ip = exit_doc.get("ip")
                raw_country = exit_doc.get("country")
                exit_ip = str(raw_ip) if raw_ip else None
                exit_country = str(raw_country) if raw_country else None
            raw_forwards = doc.get("forwarded_ports")
            forwards = (
                tuple(str(spec) for spec in raw_forwards) if isinstance(raw_forwards, list) else ()
            )
            raw_error = doc.get("last_error")
            return InstanceStatus(
                name=name,
                state=state,
                image=str(doc["image"]) if doc.get("image") else None,
                selection=_selection_from_doc(doc.get("selection")),
                drift=bool(doc["drift"]),
                control_port=_control_port(doc),
                control_enabled=control_enabled,
                exit_ip=exit_ip,
                exit_country=exit_country,
                leak=bool(doc["leak"]),
                verified=bool(doc["verified"]),
                last_error=str(raw_error) if raw_error else None,
                forwarded_ports=forwards,
                consumers=tuple(discovery.consumers_of(name)),
                saved_selection=_saved_selection(name),
                tunnel=tunnel,
                dns=dns,
                port_forward=port_forward,
            )

    # -- credentials and servers ------------------------------------------

    def credentials(self) -> list[CredentialChoice]:
        env = build_env(None)
        return [
            CredentialChoice(
                provider,
                protocol,
                tuple(var for var in config.required_env if not env.get(var)),
            )
            for provider, protocols in PROVIDERS.items()
            for protocol, config in protocols.items()
        ]

    def api_key_present(self) -> bool:
        return bool(build_env(None).get("HTTP_CONTROL_SERVER_API_KEY"))

    def servers(self) -> list[ServerOption]:
        env = build_env(None)
        image = env.get(IMAGE_ENV_VAR) or DEFAULT_IMAGE
        listing = servers.listable_servers(
            servers.get_servers(image=image),
            get_active_providers_in(env),
        )
        return [
            ServerOption(provider, protocol, country, city, hostname)
            for provider, protocol, country, city, hostname in servers.sorted_server_rows(listing)
        ]

    # -- lifecycle ---------------------------------------------------------

    def start(self, request: StartRequest, reporter: Reporter) -> ActionResult:
        inst = _common._resolve_for_command(request.name, request.ctl_port, request.env_file)
        saved = inst.saved_selection
        effective = _resolve_forwarded(
            inst,
            request.publish_specs,
            request.append_publish_specs,
            request.no_publish,
        )
        previous_forwards = set(inst.forwarded_ports)
        if set(effective) != previous_forwards:
            inst = replace(inst, forwarded_ports=effective)
        if (
            request.ctl_port is None
            and not inst.env.get(CTL_PORT_ENV_VAR)
            and read_registry(inst.name) is None
        ):
            if docker.container_running(name=inst.name):
                published = docker.container_control_port(name=inst.name)
                if published is not None and published != inst.control_port:
                    inst = _replace_port(inst, published)
            else:
                inst = _replace_port(
                    inst,
                    allocate_free_port(
                        exclude=frozenset(
                            host for spec in effective if (host := _host_port(spec)) is not None
                        )
                    ),
                )
        with instance_context(inst):
            ensure_compose_file(inst)
            _common.require_api_key()
            requested = any(
                value is not None
                for value in (
                    request.provider,
                    request.protocol,
                    request.country,
                    request.city,
                )
            )
            was_running = docker.container_running()
            current = _common.effective_selection() if was_running else None
            created = not was_running
            recreate = request.recreate
            if request.pull:
                reporter.say(f"Pulling {image_ref()}...")
                docker.run(
                    "docker",
                    "pull",
                    image_ref(),
                    capture=True,
                    timeout=PULL_TIMEOUT_S,
                )
                recreate = True
            announce_recreate = False
            if set(effective) != previous_forwards and was_running and not recreate:
                recreate = True
                announce_recreate = True
            if recreate:
                created = True
            if created:
                requested = request.country is not None or request.city is not None
            if was_running and not recreate and current is None:
                reporter.say("Waiting for control server...")
                current = _common.await_selection()
            if was_running and not recreate and current is None:
                raise click.ClickException(
                    "Cannot read runtime settings — is the control server reachable?"
                )

            target = current or Selection("", "")
            swapped = False
            prev_ip: str | None = None
            if created:
                if announce_recreate:
                    reporter.say(
                        "Published ports changed — recreating container "
                        "(runtime selection reverts to baked config)."
                    )
                name = (
                    request.provider
                    or (saved.provider if saved else None)
                    or (current.provider if current else None)
                )
                if not name:
                    raise click.UsageError("--provider is required to start the container.")
                saved_protocol = (
                    saved.protocol if saved and fold(saved.provider) == fold(name) else None
                )
                name, proto = resolve_provider(
                    name,
                    request.protocol or saved_protocol,
                    current.protocol if current else None,
                )
                overrides = get_provider_env(name, proto)
                if (
                    saved
                    and fold(saved.provider) == fold(name)
                    and fold(saved.protocol) == fold(proto)
                    and request.country is None
                    and request.city is None
                ):
                    overrides["SERVER_COUNTRIES"] = saved.country or ""
                    overrides["SERVER_CITIES"] = saved.city or ""
                _check_forward_collisions(inst, effective)
                ensure_compose_file(inst)
                docker.compose(
                    "up",
                    "-d",
                    *(("--force-recreate",) if recreate else ()),
                    env_overrides=overrides,
                    timeout=COMPOSE_TIMEOUT_S,
                    capture=True,
                )
                write_registry(inst)
                reporter.say(f"VPN {'recreated' if recreate else 'started'} ({name}/{proto}).")
                if effective:
                    reporter.say(f"Forwarded ports: {', '.join(effective)}.")
                current = _common._baked_selection() or Selection(name, proto)

            if requested:
                if not created:
                    prev_ip = ipinfo.current_exit_ip()
                else:
                    reporter.say("Waiting for control server...")
                    with contextlib.suppress(ControlError):
                        control.wait_for_settings()
                base = current or Selection("", "")
                target, swapped = _common._apply_request(
                    request.provider,
                    request.protocol,
                    request.country,
                    request.city,
                    base,
                )
                verb = "Swapped to" if swapped else "Already on"
                reporter.say(f"{verb} {_common._print_target(target)}.")
            elif current is not None:
                target = current

            verification = apply.verify(target, prev_ip if swapped else None)
            if not verification.ok:
                return ActionResult(
                    False,
                    f"Connection unverified ({verification.reason}).",
                    detail=f"Target: {_common._print_target(target)}",
                )
            if request.speedtest:
                _run_speedtest(request.size, reporter)
            sync_registry(inst)
            return ActionResult(
                True,
                f"Connected to {_common._print_target(target)}.",
                detail=f"Exit IP: {verification.ip or 'unknown'}",
            )

    def connect(self, request: ConnectRequest, reporter: Reporter) -> ActionResult:
        inst = _common._resolve_for_command(request.name)
        with instance_context(inst):
            _common.require_api_key()
            if not docker.container_running():
                raise click.ClickException(
                    f"Container '{current_instance().container}' is not running. Use Start first."
                )
            current = _common._require_selection()
            prev_ip = ipinfo.current_exit_ip()
            target, swapped = _common._apply_request(
                request.provider,
                request.protocol,
                request.country,
                request.city,
                current,
            )
            verb = "Swapped to" if swapped else "Already on"
            reporter.say(f"{verb} {_common._print_target(target)}.")
            verification = apply.verify(target, prev_ip if swapped else None)
            if not verification.ok:
                return ActionResult(
                    False,
                    f"Connection unverified ({verification.reason}).",
                    detail=f"Target: {_common._print_target(target)}",
                )
            if request.speedtest:
                _run_speedtest(request.size, reporter)
            return ActionResult(
                True,
                f"Connected to {_common._print_target(target)}.",
                detail=f"Exit IP: {verification.ip or 'unknown'}",
            )

    def rotate(self, request: RotateRequest, reporter: Reporter) -> ActionResult:
        inst = _common._resolve_for_command(request.name)
        if request.save and read_registry(inst.name) is None:
            raise click.ClickException(
                f"Instance '{inst.name}' has no registry record; cannot save a location "
                "for a container epoxy did not create."
            )
        with instance_context(inst):
            _common.require_api_key()
            if not docker.container_running():
                raise click.ClickException(
                    f"Container '{current_instance().container}' is not running. Use Start first."
                )
            current = _common._require_selection()
            by_provider = servers.listable_servers(servers.get_servers())
            if not any(by_provider.values()):
                raise click.ClickException("No servers found. Is Docker running?")
            rows = next(
                (
                    found
                    for provider, found in by_provider.items()
                    if fold(provider) == fold(current.provider)
                ),
                [],
            )
            pool = location_pool(
                rows,
                current.provider,
                current.protocol,
                request.country,
                {folded_location(current.country, current.city)},
            )
            if not pool:
                where = f" in {request.country}" if request.country else ""
                raise click.ClickException(
                    f"No alternate {current.provider}/{current.protocol} servers{where}."
                )
            attempts = shuffle_locations(pool, random.SystemRandom())[:ROTATE_ATTEMPTS]
            prev_ip = ipinfo.current_exit_ip()
            reason = "exit IP did not change"
            for index, (country_name, city) in enumerate(attempts):
                target = Selection(current.provider, current.protocol, country_name, city)
                try:
                    apply.apply_location(target)
                except ControlError as exc:
                    raise click.ClickException(
                        f"Could not switch to {_common._print_target(target)} — {exc.message}"
                    ) from exc
                verdict = apply.verify(target, prev_ip)
                if verdict.ok:
                    reporter.say(
                        f"Swapped to {_common._print_target(target)} — "
                        f"exit {verdict.ip or 'unknown'}."
                    )
                    if verdict.geo:
                        reporter.say(f"note: exits via {verdict.geo} (requested {target.country})")
                    if request.save:
                        saved = SavedSelection(
                            target.provider, target.protocol, target.country, target.city
                        )
                        if not update_saved_selection(inst.name, saved):
                            raise click.ClickException(
                                f"Instance '{inst.name}' has no registry record; could not save."
                            )
                        reporter.say(
                            "Saved as instance config (applied on the next create/recreate)."
                        )
                    if request.speedtest:
                        _run_speedtest(request.size, reporter)
                    return ActionResult(
                        True,
                        f"Rotated to {_common._print_target(target)}.",
                        detail=f"Exit IP: {verdict.ip or 'unknown'}",
                    )
                reason = verdict.reason or reason
                if index < len(attempts) - 1:
                    reporter.say(f"{reason}; trying another location.")
            raise click.ClickException(
                f"Could not move the exit IP after {len(attempts)} "
                f"tr{'y' if len(attempts) == 1 else 'ies'} ({reason})."
            )

    def stop(self, name: str, reporter: Reporter) -> ActionResult:
        inst = _common._resolve_for_command(name)
        with instance_context(inst):
            with contextlib.suppress(Exception):
                control.set_tunnel_status("stopped", timeout=3)
            docker.compose("down", timeout=COMPOSE_TIMEOUT_S, capture=True)
        return ActionResult(True, f"Stopped '{name}'.")

    def remove(self, name: str, force: bool, reporter: Reporter) -> ActionResult:
        inst = _common._resolve_for_command(name)
        if name not in discovery.known_names():
            raise click.ClickException(f"Unknown instance '{name}'.")
        consumers = discovery.consumers_of(name)
        if consumers and not force:
            raise click.ClickException(
                f"Instance '{name}' is shared by: {', '.join(consumers)}. "
                "Force removal to continue."
            )
        with instance_context(inst):
            with contextlib.suppress(Exception):
                control.set_tunnel_status("stopped", timeout=3)
            if os.path.exists(inst.compose_file):
                docker.compose("down", timeout=COMPOSE_TIMEOUT_S, capture=True)
            else:
                docker.remove_container(name)
        delete_instance_state(name)
        return ActionResult(True, f"Removed '{name}'.")

    def toggle_dns(self, name: str, reporter: Reporter) -> ActionResult:
        inst = _common._resolve_for_command(name)
        with instance_context(inst):
            try:
                current = control.get_dns_status()
                target = "stopped" if current == "running" else "running"
                control.set_dns_status(target)
            except ControlError as exc:
                raise click.ClickException(f"Cannot reach control server: {exc.message}") from exc
        return ActionResult(True, f"DNS {target} for '{name}'.")

    def update_servers(self, name: str, reporter: Reporter) -> ActionResult:
        inst = _common._resolve_for_command(name)
        with instance_context(inst):
            try:
                control.trigger_updater()
            except ControlError as exc:
                raise click.ClickException(f"Cannot reach control server: {exc.message}") from exc
        return ActionResult(True, f"Server list update triggered for '{name}'.")

    def speed_test(self, name: str, size: int, reporter: Reporter) -> ActionResult:
        inst = _common._resolve_for_command(name)
        with instance_context(inst):
            result = _run_speedtest(size, reporter)
        if result is None:
            return ActionResult(False, f"Speed test failed for '{name}'.")
        return ActionResult(True, f"Speed test: {speedtest.format_result(result)}")

    def bench(
        self,
        request: BenchRequest,
        reporter: Reporter,
        stop_event: threading.Event,
    ) -> BenchOutcome:
        inst = _common._resolve_for_command(request.name)
        with instance_context(inst):
            _common.require_api_key()
            if not docker.container_running():
                raise click.ClickException(
                    f"Container '{current_instance().container}' is not running."
                )
            by_provider = servers.listable_servers(servers.get_servers())
            if not any(by_provider.values()):
                raise click.ClickException("No servers found. Is Docker running?")
            try:
                control.get_settings()
            except ControlError as exc:
                raise click.ClickException(
                    f"Cannot reach the control server: {exc.message}"
                ) from None
            candidates = build_candidates(
                by_provider, request.provider, request.protocol, request.country
            )
            if not candidates:
                raise click.ClickException("No matching locations for the given filters.")

            def say(line: str) -> None:
                if stop_event.is_set():
                    raise KeyboardInterrupt
                reporter.say(line)

            report = run_bench(
                candidates,
                limit=request.max_candidates,
                top=request.top,
                scan_size_mb=request.scan_size,
                final_size_mb=request.size,
                concurrency=request.concurrency,
                connect_winner=request.connect_winner,
                say=say,
            )
        ok = not report.interrupted
        message = report.action or "Benchmark complete."
        return BenchOutcome(ActionResult(ok, message), report)

    def stream_logs(
        self,
        name: str,
        tail: int,
        follow: bool,
        on_line: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None:
        inst = _common._resolve_for_command(name)
        with instance_context(inst):
            command = [
                "docker",
                "compose",
                "-f",
                inst.compose_file,
                "-p",
                inst.project,
                "logs",
            ]
            if follow:
                command.append("--follow")
            command.extend(["--tail", str(tail), COMPOSE_SERVICE])
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env={**os.environ, **inst.env},
            )
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    if stop_event.is_set():
                        break
                    on_line(line.rstrip("\n"))
            finally:
                if process.poll() is None:
                    process.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=2)

    def save_current(self, name: str, reporter: Reporter) -> ActionResult:
        inst = _common._resolve_for_command(name)
        with instance_context(inst):
            current = _common._require_selection()
        saved = SavedSelection(current.provider, current.protocol, current.country, current.city)
        if not update_saved_selection(name, saved):
            raise click.ClickException(f"Instance '{name}' has no registry record; could not save.")
        return ActionResult(True, f"Saved {_common._print_target(current)} for '{name}'.")

    def clear_saved(self, name: str, reporter: Reporter) -> ActionResult:
        had_saved = _saved_selection(name) is not None
        if not update_saved_selection(name, None):
            raise click.ClickException(
                f"Instance '{name}' has no registry record; nothing to clear."
            )
        return ActionResult(
            True,
            "Saved location cleared." if had_saved else "No saved location.",
        )


def _replace_port(inst: Instance, port: int) -> Instance:
    """Copy an instance with a different control port."""
    return replace(inst, control_port=port)


def _host_port(spec: str) -> int | None:
    from epoxy.publish import spec_host_port

    return spec_host_port(spec)


def _run_speedtest(size: int, reporter: Reporter) -> speedtest.Result | None:
    reporter.say(f"Running speed test ({size} MB)...")
    result = speedtest.measure(size, on_progress=reporter.progress)
    if result is None:
        reporter.say("Speed test failed.")
        return None
    reporter.say(speedtest.format_result(result))
    return result


__all__ = [
    "ActionResult",
    "Backend",
    "BenchOutcome",
    "BenchRequest",
    "ConnectRequest",
    "CredentialChoice",
    "InstanceStatus",
    "InstanceSummary",
    "RealBackend",
    "Reporter",
    "RotateRequest",
    "ServerOption",
    "SilentReporter",
    "StartRequest",
    "describe_exception",
]
