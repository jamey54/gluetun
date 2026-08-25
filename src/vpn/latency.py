"""Host-side TCP connect latency probes for prescreening locations."""

import socket
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from vpn.config import LATENCY_PORT, LATENCY_TIMEOUT_S

MAX_WORKERS = 32


def probe_host(
    host: str, port: int = LATENCY_PORT, timeout: float = LATENCY_TIMEOUT_S
) -> float | None:
    """TCP handshake time in seconds, or None if the host is unreachable."""
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return None
    return time.monotonic() - start


def probe_hosts(
    hosts: Iterable[str],
    port: int = LATENCY_PORT,
    timeout: float = LATENCY_TIMEOUT_S,
    max_workers: int = MAX_WORKERS,
) -> dict[str, float | None]:
    """Probe unique hosts in parallel; maps each host to seconds (None if unreachable)."""
    unique = sorted(set(hosts))
    if not unique:
        return {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(unique))) as pool:
        results = pool.map(lambda h: probe_host(h, port, timeout), unique)
        return dict(zip(unique, results, strict=True))
