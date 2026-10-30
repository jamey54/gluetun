"""Pure view-model helpers for the TUI (no Textual, no Docker).

Keeping filtering, target resolution, and rendering here makes the UI easy to
test without a terminal and keeps the screen classes about layout and events.
"""

from __future__ import annotations

from datetime import datetime

from rich.markup import escape

from epoxy.bench import BenchReport, sorted_results
from epoxy.textutil import fold
from epoxy.tui.backend import CredentialChoice, InstanceStatus, InstanceSummary

STATE_STYLES = {
    "running": "green",
    "starting": "yellow",
    "stopped": "dim",
    "absent": "red",
}

STATE_SYMBOLS = {
    "running": "●",
    "starting": "◐",
    "stopped": "○",
    "absent": "✕",
}


def state_style(state: str) -> str:
    return STATE_STYLES.get(state, "")


def state_symbol(state: str) -> str:
    return STATE_SYMBOLS.get(state, "?")


def filter_summaries(rows: list[InstanceSummary], query: str) -> list[InstanceSummary]:
    """Filter by any visible field, accent-insensitively; empty query keeps all."""
    tokens = [token for token in (fold(part) for part in query.split()) if token]
    if not tokens:
        return list(rows)
    matched: list[InstanceSummary] = []
    for row in rows:
        haystack = fold(
            " ".join(
                [
                    row.name,
                    row.state,
                    row.selection_label,
                    " ".join(row.consumers),
                ]
            )
        )
        if all(token in haystack for token in tokens):
            matched.append(row)
    return matched


def resolve_targets(
    selected: set[str],
    cursor: str | None,
    visible: list[str],
) -> list[str]:
    """Actions apply to the multi-selection, else the highlighted row."""
    if selected:
        return sorted(selected)
    if cursor and cursor in visible:
        return [cursor]
    return []


def parse_publish_specs(text: str) -> tuple[str, ...]:
    """Comma/space separated docker short specs; blank means no explicit set."""
    return tuple(part for part in text.replace(",", " ").split() if part)


def parse_optional_int(text: str, label: str) -> int | None:
    """Parse an optional positive integer with a friendly error."""
    value = text.strip()
    if not value:
        return None
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f"{label} must be a number.") from None
    if number < 1:
        raise ValueError(f"{label} must be at least 1.")
    return number


def format_started(value: str | None) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _kv(label: str, value: str) -> str:
    return f"[bold]{label:<11}[/bold]{value}"


def detail_markup(status: InstanceStatus) -> str:
    """Rich-markup detail panel for the selected instance."""
    state = f"[{state_style(status.state)}]{state_symbol(status.state)} {status.state}[/]"
    lines = [f"[bold]{escape(status.name)}[/bold]   {state}", ""]
    lines.append(_kv("Image", escape(status.image or "-")))
    lines.append(_kv("Provider", escape(status.selection_label)))
    if status.selection:
        lines.append(_kv("Protocol", escape(status.selection.protocol or "-")))
        location = ", ".join(filter(None, [status.selection.city, status.selection.country]))
        lines.append(_kv("Location", escape(location or "-")))
    exit_text = status.exit_ip or "-"
    if status.exit_country:
        exit_text += f" ({status.exit_country})"
    lines.append(_kv("Exit IP", escape(exit_text)))
    if status.leak:
        verdict = "[red]LEAK[/red]"
    elif status.verified:
        verdict = "[green]verified[/green]"
    else:
        verdict = "[yellow]unverified[/yellow]"
    lines.append(_kv("Tunnel", verdict))
    if status.drift:
        lines.append(_kv("Drift", "[yellow]runtime selection differs from baked[/yellow]"))
    lines.append(_kv("Control", f"127.0.0.1:{status.control_port}" if status.control_port else "-"))
    if status.tunnel is not None:
        lines.append(_kv("VPN", escape(status.tunnel)))
    if status.dns is not None:
        lines.append(_kv("DNS", escape(status.dns)))
    if status.port_forward is not None:
        lines.append(_kv("Port fwd", str(status.port_forward)))
    lines.append(_kv("Consumers", escape(", ".join(status.consumers) if status.consumers else "-")))
    lines.append(
        _kv(
            "Forwarded",
            escape(", ".join(status.forwarded_ports) if status.forwarded_ports else "-"),
        )
    )
    if status.saved_selection:
        saved = status.saved_selection
        location = ", ".join(filter(None, [saved.city, saved.country]))
        lines.append(_kv("Saved", escape(f"{saved.provider}/{saved.protocol} → {location}")))
    if status.last_error:
        lines.extend(["", f"[red]{escape(status.last_error)}[/red]"])
    return "\n".join(lines)


def credential_markup(choices: list[CredentialChoice]) -> str:
    """Read-only credential readiness: names only, never values."""
    if not choices:
        return "[yellow]No providers configured.[/yellow]"
    lines = ["[bold]Credentials[/bold]", ""]
    for choice in choices:
        if choice.ready:
            lines.append(f"[green]✓[/green] {escape(choice.provider)} / {escape(choice.protocol)}")
        else:
            missing = ", ".join(choice.missing)
            lines.append(
                f"[red]✕[/red] {escape(choice.provider)} / {escape(choice.protocol)} "
                f"[dim](missing {escape(missing)})[/dim]"
            )
    return "\n".join(lines)


def bench_rows(report: BenchReport) -> list[tuple[str, ...]]:
    """Flatten a bench report into table cells, fastest first."""
    rows: list[tuple[str, ...]] = []
    for index, result in enumerate(sorted_results(report.results), 1):
        latency = f"{result.latency_s * 1000:.0f} ms" if result.latency_s is not None else "-"
        scan = f"{result.scan_mbps:.1f}" if result.scan_mbps is not None else "-"
        final = f"{result.final_mbps:.1f}" if result.final_mbps is not None else "-"
        if result.final_mbps is not None:
            status = "final"
        elif result.scan_mbps is not None:
            status = "ok"
        else:
            status = result.error or "-"
        if result.scan_mbps is not None and result.actual_geo:
            status += f" · geo: {result.actual_geo}"
        marker = "★ " if result is report.winner else ""
        rows.append(
            (
                str(index),
                result.candidate.provider,
                result.candidate.protocol,
                f"{marker}{result.candidate.location}",
                latency,
                scan,
                final,
                status,
            )
        )
    return rows
