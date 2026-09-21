"""Show container state, effective selection, public IP, and speed test."""

import json

import click

from epoxy import control, discovery, docker, ipinfo
from epoxy.apply import Selection
from epoxy.commands import _common
from epoxy.commands._common import (
    _baked_selection,
    _resolve_for_command,
    _resolve_targets,
    add_instance_options,
    all_option,
    for_each_instance,
)
from epoxy.config import DEFAULT_SIZE_MB, JsonDoc
from epoxy.countries import resolve_country
from epoxy.instance import Instance, current_instance, instance_context
from epoxy.statusdoc import classify_verdict, control_server_doc, selection_doc


def _status_doc() -> JsonDoc:
    """Stable status document (status --json). Called inside the instance context.

    Contract (README §"Machine-readable output"): exit code is 0 for a healthy
    or simply-stopped instance, 1 when the tunnel verifiably leaks or the
    control server is unreachable while the container runs/restarts. Probe
    failure is probe health, never a leak: it is reported in ``last_error``
    with ``leak: false`` and exit 0 (C8).
    """
    inst = current_instance()
    state = discovery._state(inst.name)
    sel: Selection | None = None
    enabled = False
    last_error: str | None = None
    if state in ("running", "starting"):
        try:
            sel = Selection.from_doc(control.get_settings())
        except control.ControlError as exc:
            status = f"HTTP {exc.status}" if exc.status is not None else "connection failed"
            last_error = f"control server unreachable ({status}): {exc.message}"
        enabled = bool(sel and sel.provider)

    baked = _baked_selection() if state in ("running", "starting") else None
    drift = bool(sel is not None and sel.provider and baked is not None and sel.key != baked.key)

    exit_ip: dict[str, str | None] | None = None
    leak = False
    verified = False
    if state == "running":
        result = ipinfo._probe()
        if result is None:
            if last_error is None:
                last_error = "could not determine the exit IP (all echo services failed)"
        else:
            info = result.info
            ip = str(info.get("ip") or "")
            if not ip:
                if last_error is None:
                    last_error = "could not determine the exit IP (empty observation)"
            else:
                exit_ip = {
                    "ip": ip,
                    "country": _clean_country(info.get("country")),
                }
                verdict, verified = classify_verdict(ipinfo.real_ip(), ip)
                leak = verdict == "leak"
                if verdict == "unknown" and last_error is None:
                    last_error = "could not determine the host's bare IP — tunnel unverified"
    return {
        "instance": inst.name,
        "container_name": inst.name,
        "image": docker.container_image() if state != "absent" else None,
        "state": state,
        "selection": selection_doc(sel) if sel and sel.provider else None,
        "drift": drift,
        "control_server": control_server_doc(inst.control_port, enabled),
        "exit_ip": exit_ip,
        "leak": leak,
        "verified": verified,
        "last_error": last_error,
    }


def _clean_country(value: object) -> str | None:
    """Normalize a raw country field: missing/''/'None'/'null' become None (C3)."""
    text = str(value or "").strip()
    if not text or text.lower() in ("none", "null"):
        return None
    return resolve_country(text) or None


def _kv(label: str, value: str, color: str | None = None) -> None:
    """Print an indented key-value line with a bold label."""
    text = f"  {label:<12}{value}"
    if color:
        click.echo(click.style(text, fg=color))
    else:
        click.echo(text)


def _status_json_failed(doc: JsonDoc) -> bool:
    """Whether a status document trips the exit-1 rules: leak, or unreachable
    control server while the container runs/restarts."""
    if doc["leak"]:
        return True
    control_doc = doc["control_server"]
    return (
        doc["state"] in ("running", "starting")
        and isinstance(control_doc, dict)
        and not control_doc.get("enabled")
        and bool(doc["last_error"])
    )


def _print_human_status(size: int, no_speedtest: bool) -> None:
    """Human status body for the active instance (raises on failure, as before)."""
    state = docker.container_status()
    if not state:
        click.echo(f"Container '{current_instance().container}' not found.")
        raise click.ClickException(f"Container '{current_instance().container}' is not running.")

    container = current_instance().container
    _kv("Container", f"{container} ({state})")

    try:
        tunnel = control.get_tunnel_status()
        _kv("Tunnel", tunnel, "red" if tunnel == "stopped" else None)
    except control.ControlError:
        pass
    try:
        dns = control.get_dns_status()
        _kv("DNS", dns, "red" if dns == "stopped" else None)
    except control.ControlError:
        pass
    try:
        port = control.get_port_forward()
        if port:
            _kv("Port fwd", str(port))
    except control.ControlError:
        pass

    current, reachable = _common._runtime_selection_or_error()
    if current and current.provider:
        click.echo()
        _kv("Provider", current.provider)
        _kv("Protocol", current.protocol or "?")
        if current.country:
            loc = ", ".join(filter(None, [current.city, current.country]))
            _kv("Location", loc)
    else:
        click.echo()
        _kv("Provider", "unknown — is the control server reachable?")

    if state == "running":
        click.echo()
        verified = _common.finish_connection(
            expected_country=current.country if current and current.country else None,
            run_speedtest=not no_speedtest,
            size=size,
        )
        if not verified:
            raise SystemExit(1)
    if state in ("running", "starting") and not reachable:
        raise SystemExit(1)


@click.command()
@add_instance_options()
@all_option
@click.option(
    "-s",
    "--size",
    type=click.IntRange(min=1),
    default=DEFAULT_SIZE_MB,
    show_default=True,
    help="Speed test download size (MB)",
)
@click.option("--no-speedtest", is_flag=True, help="Skip the speed test")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Machine-readable status (single-line JSON)",
)
def status(
    instance: str | None, size: int, no_speedtest: bool, json_output: bool, all_instances: bool
) -> None:
    """Show container state, effective selection, public IP, and speed test.

    Exit codes (M9): 0 healthy/stopped · 1 leak, unreachable control server
    (while the container runs/restarts), or verification failure (human mode)
    · 2 usage. ``--json`` exits 0 on an inconclusive probe — probe health is
    reported in ``last_error``, never conflated with a leak. With ``--all``,
    failures are reported per instance and the exit code reflects the worst
    one; ``--json`` emits an ``{"instances": [...]}`` envelope.
    """
    targets = _resolve_targets(instance, all_instances)
    if all_instances:
        if json_output:
            docs = []
            for inst in targets:
                with instance_context(inst):
                    docs.append(_status_doc())
            click.echo(json.dumps({"instances": docs}))
            if any(_status_json_failed(doc) for doc in docs):
                raise SystemExit(1)
            return

        def show_one(inst: Instance, _fan_out: bool) -> None:
            with instance_context(inst):
                _print_human_status(size, no_speedtest)

        for_each_instance(targets, show_one, all_instances, header=True, separate=True)
        return
    with instance_context(_resolve_for_command(instance)):
        if json_output:
            doc = _status_doc()
            click.echo(json.dumps(doc))
            if _status_json_failed(doc):
                raise SystemExit(1)
            return
        _print_human_status(size, no_speedtest)
