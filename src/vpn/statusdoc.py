"""Shared status-document and verification vocabulary (the "status contract").

Single source of truth for the machine-readable records emitted by
``status --json`` and ``ls --json`` and for the leak/verification verdict used
by both the human and JSON status paths (C8, M1). Keeping the builders and the
verdict classifier here means the two output paths cannot drift, and the
documented schemas (README §"Machine-readable output") are enforced by the
module-level field tuples below.

Import-safe by design: no vpn module imports (``apply.Selection`` satisfies
``SelectionLike`` structurally), so any layer can depend on this module.
"""

from typing import Any, Literal, Protocol

#: Fields of a ``status --json`` document, in emission order. Keep in sync
#: with the README schema example (P3-13).
STATUS_FIELDS = (
    "instance",
    "container_name",
    "image",
    "state",
    "selection",
    "drift",
    "control_server",
    "exit_ip",
    "leak",
    "verified",
    "last_error",
)

#: Fields of one ``ls --json`` record, in emission order (P3-13).
RECORD_FIELDS = (
    "instance",
    "container_name",
    "state",
    "selection",
    "control_server",
    "consumers",
    "started_at",
)

Verdict = Literal["ok", "leak", "unknown"]


class SelectionLike(Protocol):
    """Structural view of a selection (satisfied by ``apply.Selection``)."""

    @property
    def provider(self) -> str: ...

    @property
    def protocol(self) -> str: ...

    @property
    def country(self) -> str | None: ...

    @property
    def city(self) -> str | None: ...


def selection_doc(sel: SelectionLike | None) -> dict[str, str | None] | None:
    """Stable selection document shared by ``status --json`` and ``ls --json``."""
    if sel is None:
        return None
    return {
        "provider": sel.provider,
        "protocol": sel.protocol,
        "country": sel.country,
        "city": sel.city,
    }


def control_server_doc(port: int | None, enabled: bool) -> dict[str, Any] | None:
    """Control-server document; None when no port is known (not reported)."""
    if port is None:
        return None
    return {"port": port, "enabled": enabled}


def classify_verdict(
    bare: str | None,
    vpn_ip: str | None,
    *,
    matched: bool = True,
    expected_country: str | None = None,
) -> tuple[Verdict, bool]:
    """Classify one observation into ``(state, verified)``.

    States:

    - ``leak`` — the container exits via the host's bare IP (verified False).
    - ``unknown`` — no usable observation, or the host's bare IP is unknown and
      no country match vouches for the exit. Fail-closed: a missing bare IP
      never silently yields a green verdict (C2).
    - ``ok`` — the exit differs from the bare IP, or (degraded) the requested
      country matched while the bare IP was unavailable — the caller must warn
      loudly in that degraded case.
    """
    if not vpn_ip:
        return "unknown", False
    if bare is None:
        if expected_country and matched:
            return "ok", True
        return "unknown", False
    if vpn_ip == bare:
        return "leak", False
    return "ok", True
