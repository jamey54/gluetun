"""Runtime configuration engine: selection changes via the control server.

Every provider/protocol/location change hot-swaps through gluetun's settings
route (`GET/PUT /v1/vpn/settings`) in single-digit seconds — the container is
never recreated. An advisory lockfile serializes read-modify-write round-trips
across concurrent CLI processes (e.g. a bench running while a server is
picked). Selections live at runtime only: recreating the container reverts to
the compose/.env configuration.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from vpn import config
from vpn.config import HTTP_NOT_FOUND, LOCK_FILE_PERMS
from vpn.control import ControlError, get_settings, put_settings, with_location
from vpn.countries import resolve_country
from vpn.ipinfo import fetch_ip_info, real_ip
from vpn.textutil import fold

VERIFY_RETRIES = 3
VERIFY_DELAY_S = 1

_UPGRADE_HINT = "gluetun image lacks the settings route; run 'vpn up --pull' to update"


@dataclass(frozen=True)
class Selection:
    """The VPN target currently applied to the running container."""

    provider: str
    protocol: str
    country: str | None = None
    city: str | None = None

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "Selection":
        """Parse a settings document into a Selection."""
        provider_doc = doc.get("provider") or {}
        selection = provider_doc.get("server_selection") or {}
        countries = selection.get("countries") or []
        cities = selection.get("cities") or []
        return cls(
            provider=str(provider_doc.get("name") or ""),
            protocol=str(doc.get("type") or ""),
            country=countries[0] if countries else None,
            city=cities[0] if cities else None,
        )

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Folded identity for semantic comparison (case/accent-insensitive)."""
        return (
            fold(self.provider),
            fold(self.protocol),
            fold(self.country or ""),
            fold(self.city or ""),
        )


@contextmanager
def swap_lock() -> Iterator[None]:
    """Advisory cross-process lock held across each settings mutation."""
    lock_file = config.LOCK_FILE  # read dynamically so tests can redirect it
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, LOCK_FILE_PERMS)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _translate(exc: ControlError) -> ControlError:
    """Replace a bare 404 with an actionable upgrade hint; pass others through."""
    if exc.status == HTTP_NOT_FOUND:
        return ControlError(HTTP_NOT_FOUND, _UPGRADE_HINT)
    return exc


def apply_location(sel: Selection) -> None:
    """Hot-swap the tunnel to sel via GET → mutate → PUT under the lock."""
    try:
        with swap_lock():
            put_settings(
                with_location(get_settings(), sel.provider, sel.protocol, sel.country, sel.city)
            )
    except ControlError as exc:
        raise _translate(exc) from exc


def restore_settings(doc: dict[str, Any]) -> None:
    """Put a previously captured settings document back (under the lock)."""
    try:
        with swap_lock():
            put_settings(doc)
    except ControlError as exc:
        raise _translate(exc) from exc


@dataclass
class Verification:
    """Outcome of post-swap verification."""

    ok: bool  # exit IP differs from both the bare IP and the previous exit
    ip: str | None = None  # observed exit IP, when any
    geo: str | None = None  # actual exit country when it differs from the request
    reason: str = ""  # failure label: leak / no reconnect / no public IP


def verify(sel: Selection, prev_ip: str | None = None) -> Verification:
    """Prove the tunnel moved: new exit IP, different from bare and previous.

    The bare-IP exclusion comes from ipinfo itself; prev_ip is added so a
    failed swap silently keeping the old server is caught. Country never
    gates success — a geo mismatch is surfaced as `geo`.
    """
    outcome = fetch_ip_info(
        retries=VERIFY_RETRIES,
        delay=VERIFY_DELAY_S,
        expected_country=sel.country,
        exclude_ips={prev_ip} if prev_ip else None,
    )
    if outcome.result is not None:
        info = outcome.result.info
        geo = resolve_country(str(info.get("country", "")))
        return Verification(
            ok=True,
            ip=str(info.get("ip") or "") or None,
            geo=None if outcome.result.matched else (geo or None),
        )

    last_ip = str((outcome.last_info or {}).get("ip") or "")
    bare = real_ip()
    reason = "no public IP"
    if last_ip and bare and last_ip == bare:
        reason = "leak"
    elif last_ip and prev_ip and last_ip == prev_ip:
        reason = "no reconnect"
    return Verification(ok=False, ip=last_ip or None, reason=reason)
