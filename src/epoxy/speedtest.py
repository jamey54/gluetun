"""Download speed test through the VPN container."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from epoxy.config import DEFAULT_SIZE_MB, DOWNLOAD_TIMEOUT_S, SPEEDTEST_URL
from epoxy.docker import run_streamed
from epoxy.instance import current_instance

# Called with (downloaded_bytes, total_bytes) as the payload streams in.
ProgressFn = Callable[[int, int], None]


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
    *,
    on_progress: ProgressFn | None = None,
) -> Result | None:
    """Download size_mb through a container. Returns a Result, or None on failure.

    The payload is streamed to a pipe and discarded, so the only way to watch a
    download is on_progress: it is called with (downloaded, total) as bytes
    arrive, which is what a progress bar needs to show real progress. Pass
    nothing (bench does) and the download stays silent.
    """
    container = container or current_instance().container
    nbytes = size_mb * 1_000_000
    downloaded = 0
    start = time.monotonic()

    def _count(size: int) -> None:
        nonlocal downloaded
        downloaded += size
        if on_progress is not None:
            on_progress(downloaded, nbytes)

    returncode = run_streamed(
        "docker",
        "exec",
        container,
        "timeout",
        str(timeout),
        "wget",
        "-q",
        # -O- streams the payload into our pipe instead of /dev/null, so the
        # bytes can be counted as they arrive; the exec still reports wget's
        # exit code, because wget is the command the shell-less exec runs. The
        # single-token spelling matches ipinfo.probe_provider, so the download
        # uses the wget invocation this image is already known to accept.
        "-O-",
        SPEEDTEST_URL.format(n=nbytes),
        on_chunk=_count,
        check=False,
        # Bound the docker exec itself, not just the in-container wget.
        timeout=timeout + 10,
    )
    seconds = time.monotonic() - start
    if returncode != 0:
        return None
    if seconds <= 0:
        seconds = 1e-9  # never divide by zero; an instant exit is still a valid download
    return Result(mbits=mbps(nbytes, seconds), seconds=seconds, mbytes=float(size_mb))
