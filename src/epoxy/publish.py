"""Published-port specs for `up --publish` (forward joiner services locally).

Syntax is docker short syntax: ``[IP:]HOST:CONTAINER``, or bare ``PORT``.
Parsing splits from the right so IPv6 bind IPs (``[::1]:8080:80``) survive.
The canonical stored form is always ``IP:HOST:CONTAINER``; a bare port binds
localhost (``127.0.0.1``), so LAN exposure is always explicit — matching the
control port precedent. Malformed specs are usage errors naming the bad value.
"""

import click

from epoxy.config import PORT_MAX, PORT_MIN

#: Bind address assumed for specs without one (bare ``PORT`` / ``HOST:CONTAINER``).
DEFAULT_BIND_IP = "127.0.0.1"


def _parse_port(text: str, spec: str) -> int:
    """One numeric port in range; usage error naming the spec otherwise."""
    text = text.strip()
    if not text.isdigit():
        raise click.UsageError(
            f"Invalid --publish spec {spec!r}: ports must be {PORT_MIN}-{PORT_MAX}."
        )
    port = int(text)
    if not PORT_MIN <= port <= PORT_MAX:
        raise click.UsageError(
            f"Invalid --publish spec {spec!r}: ports must be {PORT_MIN}-{PORT_MAX}."
        )
    return port


def parse_publish_spec(spec: str) -> tuple[str, int, int]:
    """Parse one ``--publish`` spec into ``(bind_ip, host_port, container_port)``."""
    text = spec.strip()
    rest, sep, container_text = text.rpartition(":")
    if not sep:
        port = _parse_port(text, spec)
        return DEFAULT_BIND_IP, port, port
    container_port = _parse_port(container_text, spec)
    bind_ip, sep2, host_text = rest.rpartition(":")
    if not sep2:
        return DEFAULT_BIND_IP, _parse_port(rest, spec), container_port
    host_port = _parse_port(host_text, spec)
    ip = bind_ip.strip().strip("[]")
    if not ip or any(char.isspace() for char in ip):
        raise click.UsageError(f"Invalid --publish spec {spec!r}: bad bind address.")
    return ip, host_port, container_port


def canonical_spec(bind_ip: str, host_port: int, container_port: int) -> str:
    """Canonical stored form: always ``IP:HOST:CONTAINER``."""
    return f"{bind_ip}:{host_port}:{container_port}"


def canonicalize(spec: str) -> str:
    """Parse and return the canonical form of one spec (usage error if bad)."""
    return canonical_spec(*parse_publish_spec(spec))


def spec_host_port(canonical: str) -> int | None:
    """Host port of a stored canonical spec; None when unparseable (defensive).

    Stored records predate validation, so readers must never crash on them.
    """
    try:
        _, host_port, _ = parse_publish_spec(canonical)
    except click.UsageError:
        return None
    return host_port
