"""Docker / docker compose helpers."""

import os
import subprocess
import tempfile
from pathlib import Path

from vpn.config import COMPOSE_FILE, CONTAINER, read_env_file

GLUETUN_IMAGE = "qmcgaw/gluetun:latest"


def run(*args, capture=False, check=True):
    """Run a command with argv-style arguments. Returns CompletedProcess."""
    result = subprocess.run(
        args,
        capture_output=capture,
        text=True,
    )
    if check and result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise SystemExit(f"Error: {msg}" if msg else f"Command failed ({result.returncode})")
    return result


def inspect_container(format_string):
    """Inspect the container with a Go template. None if the container doesn't exist."""
    result = run(
        "docker",
        "inspect",
        "--format",
        format_string,
        CONTAINER,
        capture=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def container_status():
    """Return the container's Docker state ('running', 'exited', ...), or None."""
    out = inspect_container("{{.State.Status}}")
    return out.strip() if out else None


def container_running():
    """True only when the container exists and is running."""
    return container_status() == "running"


def compose(*args, env_overrides=None):
    """Run docker compose with the vpn.yml file."""
    env_path = Path(COMPOSE_FILE).parent / ".env"
    merged = read_env_file(env_path)
    if env_overrides:
        merged.update(env_overrides)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        for k, v in merged.items():
            f.write(f"{k}={v}\n")
        f.flush()
        try:
            cmd = ["docker", "compose", "-f", COMPOSE_FILE, "--env-file", f.name, *args]
            return run(*cmd)
        finally:
            os.unlink(f.name)


def get_current_vpn():
    """Read (provider, VPN_TYPE) from the running container, or None."""
    out = inspect_container("{{range .Config.Env}}{{println .}}{{end}}")
    if not out:
        return None
    provider = protocol = None
    for line in out.splitlines():
        if line.startswith("VPN_SERVICE_PROVIDER="):
            provider = line.split("=", 1)[1] or None
        elif line.startswith("VPN_TYPE="):
            protocol = line.split("=", 1)[1] or None
    if not provider:
        return None
    return provider, protocol
