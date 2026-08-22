"""Docker / docker compose helpers."""

import os
import subprocess
import tempfile
from pathlib import Path

from vpn.config import COMPOSE_FILE, CONTAINER, read_env_file

GLUETUN_IMAGE = "qmcgaw/gluetun:latest"


def run(*args, capture=False, check=True):
    """Run a command. Returns CompletedProcess."""
    result = subprocess.run(
        args if len(args) > 1 else args[0],
        shell=len(args) == 1,
        capture_output=capture,
        text=True,
    )
    if check and result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise SystemExit(f"Error: {msg}" if msg else f"Command failed ({result.returncode})")
    return result


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


def get_current_provider():
    """Read VPN_SERVICE_PROVIDER from the running container."""
    result = run(
        "docker", "inspect", "--format",
        "{{range .Config.Env}}{{println .}}{{end}}", CONTAINER,
        capture=True, check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("VPN_SERVICE_PROVIDER="):
            return line.split("=", 1)[1]
    return None
