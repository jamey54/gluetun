"""Instance discovery for `vpn ls`: registry plus `vpn-*` compose containers.

Discovery only ever matches exact container names — it never reaches for "any
gluetun container" (that is what would let vpn touch a foreign gluetun).
Consumers are containers sharing the instance's network bridge
(``NetworkMode == container:<name>``).
"""

from typing import cast

from vpn import control
from vpn.apply import Selection
from vpn.config import CONTAINER_OP_TIMEOUT_S
from vpn.docker import container_control_port, container_status, run
from vpn.instance import instance_context, list_registry, read_registry, resolve_instance

PROJECT_PREFIX = "vpn-"
PROJECT_LABEL = '{{.Label "com.docker.compose.project"}}'
NETWORK_FORMAT = "{{.Names}}\t{{.HostConfig.NetworkMode}}"


def _state(name: str) -> str:
    """Docker status mapped to the stable states: running|starting|stopped|absent."""
    status = container_status(name)
    if status is None:
        return "absent"
    if status == "running":
        return "running"
    if status == "restarting":
        return "starting"
    return "stopped"


def _compose_projects() -> list[str]:
    """Compose project names of all containers (empty when docker is unavailable)."""
    try:
        result = run(
            "docker",
            "ps",
            "-a",
            "--format",
            PROJECT_LABEL,
            capture=True,
            check=False,
            timeout=CONTAINER_OP_TIMEOUT_S,
        )
    except OSError:
        return []
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def _known_names() -> set[str]:
    """Instance names: the registry plus containers under a vpn-* project."""
    names = set(list_registry())
    for project in _compose_projects():
        if project.startswith(PROJECT_PREFIX) and project[len(PROJECT_PREFIX) :]:
            names.add(project[len(PROJECT_PREFIX) :])
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


def selection_doc(sel: Selection) -> dict[str, str | None]:
    """Stable selection document shared by status --json and ls --json."""
    return {
        "provider": sel.provider,
        "protocol": sel.protocol,
        "country": sel.country,
        "city": sel.city,
    }


def consumers_of(name: str) -> list[str]:
    """Containers sharing this instance's network, sorted."""
    try:
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
    except OSError:
        return []
    target = f"container:{name}"
    consumers = []
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].strip() == target:
            consumers.append(parts[0].strip())
    return sorted(consumers)


def instance_records() -> list[dict[str, object]]:
    """One record per known instance, aligned with the ls --json schema."""
    records: list[dict[str, object]] = []
    for name in sorted(_known_names()):
        state = _state(name)
        port = _control_port(name)
        sel = _runtime_selection(name, port) if state in ("running", "starting") else None
        records.append(
            {
                "instance": name,
                "container_name": name,
                "state": state,
                "selection": selection_doc(sel) if sel else None,
                "control_server": (
                    {"port": port, "enabled": state == "running"} if port is not None else None
                ),
                "consumers": consumers_of(name),
            }
        )
    return records


def print_ls_table(records: list[dict[str, object]]) -> None:
    """Human-readable ls output."""
    import click

    if not records:
        click.echo("(no instances)")
        return
    header = ["INSTANCE", "STATE", "CONTROL", "SELECTION", "CONSUMERS"]
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
        rows.append([str(record["instance"]), str(record["state"]), control, selection, consumers])
    widths = [max(len(cell) for cell in column) for column in zip(*[header, *rows], strict=True)]
    def dump(row: list[str]) -> None:
        click.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())

    dump(header)
    for row in rows:
        dump(row)
