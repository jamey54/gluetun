"""Download speed test through the VPN container."""

import time
from dataclasses import dataclass

from epoxy.config import DEFAULT_SIZE_MB, DOWNLOAD_TIMEOUT_S, SPEEDTEST_URL
from epoxy.docker import run
from epoxy.instance import current_instance


@dataclass(frozen=True, slots=True)
class Result:
    """One completed download, so callers read attributes instead of string keys."""

    mbits: float
    seconds: float
    mbytes: float


def mbps(nbytes: int | float, seconds: float) -> float:
    """Throughput in Mbit/s."""
    return nbytes * 8 / seconds / 1_000_000


def format_result(result: Result) -> str:
    """Render a Result as the one-line human summary."""
    return f"↓ {result.mbits:.1f} Mbit/s ({result.mbytes:.0f} MB in {result.seconds:.1f}s)"


def measure(
    size_mb: int = DEFAULT_SIZE_MB,
    timeout: int = DOWNLOAD_TIMEOUT_S,
    container: str | None = None,
) -> Result | None:
    """Download size_mb through a container. Returns a Result, or None on failure."""
    container = container or current_instance().container
    nbytes = size_mb * 1_000_000
    start = time.monotonic()
    result = run(
        "docker",
        "exec",
        container,
        "timeout",
        str(timeout),
        "wget",
        "-qO",
        "/dev/null",
        SPEEDTEST_URL.format(n=nbytes),
        check=False,
        # Bound the docker exec itself, not just the in-container wget.
        timeout=timeout + 10,
    )
    seconds = time.monotonic() - start
    if result.returncode != 0:
        return None
    if seconds <= 0:
        seconds = 1e-9  # never divide by zero; an instant exit is still a valid download
    return Result(mbits=mbps(nbytes, seconds), seconds=seconds, mbytes=float(size_mb))
