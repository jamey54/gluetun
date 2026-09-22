"""Central configuration: paths, network, timeouts, and tuning knobs.

Every magic number lives here. Modules import what they need; nothing is
duplicated. Per-instance state (name, control port, env, compose file, lock)
lives in epoxy.instance.
"""

import os
from pathlib import Path

# A JSON-ish document (status records, registry entries, probe observations).
JsonDoc = dict[str, object]

# --- Env var names (the CLI's own contract; the container's VPN_*/HTTP_* names
# --- stay untouched in epoxy.yml / control.py) ------------------------------

INSTANCE_ENV_VAR = "EPOXY_INSTANCE"
CTL_PORT_ENV_VAR = "EPOXY_CTL_PORT"
CACHE_TTL_ENV_VAR = "EPOXY_CACHE_TTL"
DEBUG_ENV_VAR = "EPOXY_DEBUG"
REAL_IP_ENV_VAR = "EPOXY_REAL_IP"
IMAGE_ENV_VAR = "EPOXY_IMAGE"

# --- Paths ---------------------------------------------------------------


def default_cache_dir() -> Path:
    """The default cache root (isolated per-test via the module attrs)."""
    return Path.home() / ".cache" / "epoxy"


CACHE_DIR = default_cache_dir()
CACHE_FILE = CACHE_DIR / "servers.json"
LOCKS_DIR = CACHE_DIR / "locks"
INSTANCES_DIR = CACHE_DIR / "instances"

# --- Caching -------------------------------------------------------------

DEFAULT_CACHE_TTL = 3600
CACHE_VERSION = 4


def cache_ttl() -> int:
    """Server cache TTL in seconds; missing/garbage values fall back to default."""
    raw = os.getenv(CACHE_TTL_ENV_VAR, "")
    try:
        return int(raw) if raw else DEFAULT_CACHE_TTL
    except ValueError:
        return DEFAULT_CACHE_TTL


# --- Container image (single source; compose/pull/server-fetch all use it) --

# Pinned stable release: :latest tracks the edge of development and can break
# unattended runs. Bump deliberately after checking the release notes.
DEFAULT_IMAGE = "qmcgaw/gluetun:v3.41.3"


def image_ref() -> str:
    """Container image ref: the EPOXY_IMAGE override or the pinned default."""
    return os.getenv(IMAGE_ENV_VAR, "") or DEFAULT_IMAGE


# --- Lock ----------------------------------------------------------------

LOCK_FILE_PERMS = 0o600

# --- HTTP / Control server -----------------------------------------------

BASE_CONTROL_PORT = 8000
GET_TIMEOUT_S = 10
PUT_TIMEOUT_S = 60
DOWN_TIMEOUT_S = 3
CONTROL_READY_RETRIES = 15
CONTROL_READY_DELAY_S = 1
HTTP_NOT_FOUND = 404

# --- Docker operations ---------------------------------------------------

COMPOSE_TIMEOUT_S = 300  # compose up/down; a stalled daemon must not hang forever
PULL_TIMEOUT_S = 600  # docker pull of the container image
CONTAINER_OP_TIMEOUT_S = 60  # disposable container launch/removal
SERVER_FETCH_TIMEOUT_S = 120  # docker run format-servers server fetch

# --- Provider ------------------------------------------------------------

DEFAULT_PROTOCOL = "wireguard"

# --- IP info / probing ---------------------------------------------------

IP_INFO_URL = "https://ipinfo.io"
IP_FETCH_RETRIES = 15
IP_FETCH_DELAY = 2
PROBE_TIMEOUT = 8
PROBE_EXEC_TIMEOUT_S = 20  # bound the docker exec itself, not just the in-container wget
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


# --- .env loading --------------------------------------------------------


def read_env_file(path: Path | str) -> dict[str, str]:
    """Parse a .env-style file into a dict.

    Skips blanks and ``#`` comments, tolerates an ``export `` prefix, keeps
    values with embedded ``=``, and strips one pair of surrounding quotes.
    """
    env: dict[str, str] = {}
    path = Path(path)
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env
