"""Runtime selection helpers shared by the CLI and the interactive UI.

These helpers talk to the active instance's control server and never print
anything. The command layer re-exports them through ``commands._common`` so the
CLI keeps its historical import surface; the TUI imports this module directly.
"""

import click

from epoxy import apply, control, docker
from epoxy.apply import Selection
from epoxy.config import DEFAULT_PROTOCOL
from epoxy.instance import current_instance, env_lookup
from epoxy.providers import validate_provider
from epoxy.textutil import fold


def require_api_key() -> None:
    """Fail closed: the control server must never run with an empty API key."""
    if not env_lookup("HTTP_CONTROL_SERVER_API_KEY"):
        port = current_instance().control_port
        raise click.ClickException(
            "HTTP_CONTROL_SERVER_API_KEY is not set.\n"
            f"It authenticates the container's control server (port {port}) "
            "— add any random string to .env."
        )


def runtime_selection_or_error() -> tuple[Selection | None, bool]:
    """(selection, reachable): the live selection, plus whether the control
    server responded at all (None/False when unreachable or blank)."""
    try:
        return Selection.from_doc(control.get_settings()), True
    except control.ControlError:
        return None, False


def effective_selection() -> Selection | None:
    """Runtime selection from the control server; None when unreachable or blank."""
    return runtime_selection_or_error()[0]


def require_selection() -> Selection:
    """The live selection, or a friendly error when the server is unreachable."""
    sel = effective_selection()
    if sel is None or not sel.provider:
        raise click.ClickException(
            "Cannot read runtime settings — is the control server reachable?"
        )
    return sel


def await_selection() -> Selection | None:
    """Wait for the control server, then re-read the runtime selection.

    A container that is running while its control server is still coming up (e.g.
    mid-restart, with ``restart: always``) is not broken yet, so give it a bounded
    grace period before the caller reports it unreachable. None when it never
    answers.
    """
    try:
        return Selection.from_doc(control.wait_for_settings())
    except control.ControlError:
        return None


def baked_selection() -> Selection | None:
    """Selection baked into the container at create time (its compose env)."""
    env = docker.container_env()
    provider = env.get("VPN_SERVICE_PROVIDER")
    if not provider:
        return None
    return Selection(
        provider,
        env.get("VPN_TYPE") or "",
        env.get("SERVER_COUNTRIES") or env.get("VPN_COUNTRY"),
        env.get("SERVER_CITIES") or env.get("VPN_CITY"),
    )


def print_target(sel: Selection) -> str:
    """Human label for a selection: ``provider/protocol → City, Country``."""
    loc = ", ".join(filter(None, [sel.city, sel.country]))
    return f"{sel.provider}/{sel.protocol}" + (f" → {loc}" if loc else "")


def apply_request(
    provider: str | None,
    protocol: str | None,
    country: str | None,
    city: str | None,
    base: Selection,
) -> tuple[Selection, bool]:
    """Resolve the requested target over base and hot-swap; return (target, swapped).

    An explicit country replaces the location outright; a lone city keeps the
    current country (and fails when there is none); switching provider drops the
    old location. Returns swapped=False when the target already matches the
    running state.
    """
    target_provider = provider or base.provider
    target_protocol = protocol or base.protocol or DEFAULT_PROTOCOL
    target_country: str | None
    target_city: str | None
    if country is not None:
        target_country, target_city = country, city
    elif city is not None:
        target_country, target_city = base.country, city
    elif fold(target_provider) != fold(base.provider):
        target_country, target_city = None, None
    else:
        target_country, target_city = base.country, base.city

    if city is not None and not target_country:
        raise click.UsageError(
            "--city needs a country to search within: pass --country, or connect "
            "to a country first."
        )

    target_provider, target_protocol = validate_provider(target_provider, target_protocol)
    target = Selection(target_provider, target_protocol, target_country, target_city)
    if target.key == base.key:
        return target, False
    try:
        apply.apply_location(target)
    except control.ControlError as exc:
        raise click.ClickException(
            f"Could not switch to {print_target(target)} — {exc.message}"
        ) from exc
    return target, True
