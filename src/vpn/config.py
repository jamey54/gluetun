"""Central configuration: paths, network, timeouts, and tuning knobs.

Every magic number lives here. Modules import what they need; nothing is
duplicated. Per-instance state (name, control port, env, compose file, lock)
lives in vpn.instance.
"""

import os
from importlib.resources import files as resource_files
from pathlib import Path

# --- Paths ---------------------------------------------------------------

CACHE_DIR = Path.home() / ".cache" / "vpn"
CACHE_FILE = CACHE_DIR / "servers.json"
LOCKS_DIR = CACHE_DIR / "locks"
INSTANCES_DIR = CACHE_DIR / "instances"

# --- Caching -------------------------------------------------------------

CACHE_TTL: int = int(os.getenv("GLUETUN_CACHE_TTL", "3600"))
CACHE_VERSION = 4

# --- Lock ----------------------------------------------------------------

LOCK_FILE_PERMS = 0o600

# --- HTTP / Control server -----------------------------------------------

DEFAULT_CONTROL_PORT = 8000
GET_TIMEOUT_S = 10
PUT_TIMEOUT_S = 60
DOWN_TIMEOUT_S = 3
HTTP_NOT_FOUND = 404

# --- Docker operations ---------------------------------------------------

COMPOSE_TIMEOUT_S = 300  # compose up/down; a stalled daemon must not hang forever
PULL_TIMEOUT_S = 600  # docker pull of the gluetun image
CONTAINER_OP_TIMEOUT_S = 60  # disposable container launch/removal

# --- Provider ------------------------------------------------------------

DEFAULT_PROTOCOL = "wireguard"

# --- IP info / probing ---------------------------------------------------

IP_INFO_URL = "https://ipinfo.io"
IP_FETCH_RETRIES = 15
IP_FETCH_DELAY = 2
PROBE_TIMEOUT = 8
REAL_IP_TIMEOUT_S = 5
CURRENT_EXIT_IP_RETRIES = 1

# --- Speed test ----------------------------------------------------------

SPEEDTEST_URL = "https://speed.cloudflare.com/__down?bytes={n}"
DEFAULT_SIZE_MB = 25
DOWNLOAD_TIMEOUT_S = 120

# --- Latency probes ------------------------------------------------------

LATENCY_PORT = 443
LATENCY_TIMEOUT_S = 2.0

# --- Bench ---------------------------------------------------------------

DEFAULT_SCAN_SIZE_MB = 10
SCAN_TIMEOUT_S = 90
DEFAULT_TEST_CONCURRENCY = 1


# --- Compose file resolution ---------------------------------------------


def resolve_compose_file() -> str:
    """Locate vpn.yml: env override, then cwd, then the packaged copy."""
    override = os.getenv("GLUETUN_COMPOSE_FILE")
    if override:
        return override
    local = Path.cwd() / "vpn.yml"
    if local.exists():
        return str(local)
    return str(resource_files("vpn").joinpath("vpn.yml"))


# --- .env loading --------------------------------------------------------


def read_env_file(path: Path | str) -> dict[str, str]:
    """Parse a .env-style file into a dict (skips blanks and # comments)."""
    env: dict[str, str] = {}
    path = Path(path)
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env
