"""Download speed test through the VPN container."""

import time

from vpn.config import CONTAINER
from vpn.docker import run

SPEEDTEST_URL = "https://speed.cloudflare.com/__down?bytes={n}"
DEFAULT_SIZE_MB = 25
DOWNLOAD_TIMEOUT_S = 120


def mbps(nbytes, seconds):
    """Throughput in Mbit/s."""
    return nbytes * 8 / seconds / 1_000_000


def format_result(result):
    return f"↓ {result['mbits']:.1f} Mbit/s ({result['mbytes']} MB in {result['seconds']:.1f}s)"


def measure(size_mb=DEFAULT_SIZE_MB, timeout=DOWNLOAD_TIMEOUT_S):
    """Download size_mb through the container. Returns dict or None on failure."""
    nbytes = size_mb * 1_000_000
    start = time.monotonic()
    result = run(
        "docker",
        "exec",
        CONTAINER,
        "timeout",
        str(timeout),
        "wget",
        "-qO",
        "/dev/null",
        SPEEDTEST_URL.format(n=nbytes),
        check=False,
    )
    seconds = time.monotonic() - start
    if result.returncode != 0:
        return None
    return {"mbits": mbps(nbytes, seconds), "seconds": seconds, "mbytes": size_mb}
