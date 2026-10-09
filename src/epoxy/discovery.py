"""Instance discovery for `epoxy ls`: registry plus backed `epoxy-*` projects.

Discovery only ever matches exact container names — it never reaches for "any
Gluetun container" (that is what would let epoxy touch a foreign Gluetun).
Consumers are containers sharing the instance's network namespace
(``NetworkMode == container:<instance>``, referenced by name or container ID).
"""

from datetime import datetime
from typing import cast

import click

from epoxy import control
from epoxy.apply import Selection
from epoxy.config import CONTAINER_OP_TIMEOUT_S, JsonDoc
from epoxy.docker import (
    container_control_port,
    container_id,
    container_started_at,
    container_status,
    run,
)
from epoxy.instance import instance_context, list_registry, read_registry, resolve_instance
from epoxy.statusdoc import control_server_doc, selection_doc

PROJECT_PREFIX = "epoxy-"
PROJECT_LABEL = '{{.Label "com.docker.compose.project"}}'
NETWORK_FORMAT = "{{.Names}}\t{{.HostConfig.NetworkMode}}"
PS_FORMAT = "{{.Names}}\t" + PROJECT_LABEL


def instance_state(name: str) -> str:
    """Docker status mapped to the stable states: running|starting|stopped|absent."""
    status = container_status(name)
    if status is None:
        return "absent"
    if status == "running":
        return "running"
    if status == "restarting":
        return "starting"
    return "stopped"


def _ps_containers() -> tuple[list[str], list[str]]:
    """Container names and compose project labels from `docker ps -a`.

    Returns two aligned lists (a container without the label yields an empty
    project entry); both are empty when docker is unavailable.
    """
    result = run(
        "docker",
        "ps",
        "-a",
        "--format",
        PS_FORMAT,
        capture=True,
        check=False,
        timeout=CONTAINER_OP_TIMEOUT_S,
    )
    names: list[str] = []
    projects: list[str] = []
    for line in (result.stdout or "").splitlines():
        name, _, project = line.partition("\t")
        names.append(name.strip())
        projects.append(project.strip())
    return names, projects


def known_names() -> set[str]:
    """Instance names: the registry, plus `epoxy-*` projects backed by evidence.

    A project label alone is not evidence: a foreign stack reusing the
    ``epoxy-`` prefix (or any leftover container with a different name) must
    not surface as a phantom instance. A label counts only when the registry
    knows the name or a container with exactly that name exists — which keeps
    registry-less (imported) containers addressable.
    """
    registered = set(list_registry())
    names = set(registered)
    containers, projects = _ps_containers()
    for project in projects:
        stripped = project[len(PROJECT_PREFIX) :] if project.startswith(PROJECT_PREFIX) else ""
        if stripped and (stripped in registered or stripped in containers):
            names.add(stripped)
    return names


def _control_port(name: str) -> int | None:
    """The instance's control port: registry value, else its published port."""
    registry = read_registry(name)
    port = registry.get("control_port") if registry else None
    if isinstance(port, int):
        return port
    return container_control_port(name)


def _runtime_selection(name: str, port: int | None) -> Selection | None:
    """Live selection from the instance's own control server (None if unreachable)."""
    with instance_context(resolve_instance(name, control_port=port)):
        try:
            sel = Selection.from_doc(control.get_settings())
        except control.ControlError:
            return None
    return sel if sel.provider else None


def consumers_of(name: str) -> list[str]:
    """Containers sharing this instance's network namespace, sorted.

    Consumers attach with ``--network container:<instance>`` (or compose
    ``network_mode: container:/service:<instance>``). Docker resolves that
    reference to the container *ID* at attach time, so a consumer's
    ``HostConfig.NetworkMode`` may be ``container:<name>`` or
    ``container:<id>`` — match the instance's name, full ID, and short ID.
    """
    refs = {name}
    full_id = container_id(name)
    if full_id:
        refs.add(full_id)
        refs.add(full_id[:12])
    result = run(
        "docker",
        "ps",
        "-a",
        "--format",
        NETWORK_FORMAT,
        capture=True,
        check=False,
        timeout=CONTAINER_OP_TIMEOUT_S,
    )
    prefix = "container:"
    consumers = []
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t")
        if (
            len(parts) == 2
            and (net := parts[1].strip()).startswith(prefix)
            and net[len(prefix) :] in refs
        ):
            consumers.append(parts[0].strip())
    return sorted(consumers)


def instance_records() -> list[JsonDoc]:
    """One record per known instance, aligned with the ls --json schema.

    Records are ordered by start time (oldest first); instances without a
    start time (absent, never started) sort last, tie-broken by name for
    determinism.
    """
    records: list[JsonDoc] = []
    for name in sorted(known_names()):
        state = instance_state(name)
        port = _control_port(name)
        sel = _runtime_selection(name, port) if state in ("running", "starting") else None
        records.append(
            {
                "instance": name,
                "container_name": name,
                "state": state,
                "selection": selection_doc(sel),
                "control_server": control_server_doc(port, sel is not None),
                "consumers": consumers_of(name),
                "started_at": container_started_at(name) if state != "absent" else None,
            }
        )
    return sorted(
        records,
        key=lambda r: (r["started_at"] is None, str(r["started_at"] or ""), str(r["instance"])),
    )


def _local_started(value: str) -> str:
    """RFC3339 (UTC) value -> local 'YYYY-MM-DD HH:MM:SS' for the human table."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def print_ls_table(records: list[JsonDoc]) -> None:
    """Human-readable ls output."""
    if not records:
        click.echo("(no instances)")
        return
    header = ["INSTANCE", "STATE", "CONTROL", "SELECTION", "CONSUMERS", "STARTED"]
    rows: list[list[str]] = []
    for record in records:
        server = record.get("control_server")
        control = str(server["port"]) if isinstance(server, dict) else "-"
        sel = record.get("selection")
        selection = ""
        if isinstance(sel, dict):
            location = ", ".join(filter(None, [sel.get("city"), sel.get("country")]))
            selection = f"{sel['provider']}/{sel['protocol']}"
            if location:
                selection += f" → {location}"
        consumers = ", ".join(cast(list[str], record.get("consumers") or [])) or "-"
        started = _local_started(str(record["started_at"])) if record.get("started_at") else "-"
        rows.append(
            [str(record["instance"]), str(record["state"]), control, selection, consumers, started]
        )
    widths = [max(len(cell) for cell in column) for column in zip(*[header, *rows], strict=True)]

    def dump(row: list[str]) -> None:
        click.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())

    dump(header)
    for row in rows:
        dump(row)
