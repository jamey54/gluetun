"""Docker / docker compose helpers."""

import os
import subprocess
from pathlib import Path

from vpn.config import COMPOSE_FILE, CONTAINER, read_env_file

GLUETUN_IMAGE = "qmcgaw/gluetun:latest"


def env_lookup(name: str) -> str | None:
    """Effective value for compose substitution: process environment wins over .env file."""
    value = os.environ.get(name)
    if value is not None:
        return value
    return read_env_file(Path(COMPOSE_FILE).parent / ".env").get(name)


def run(
    *args: str,
    capture: bool = False,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command with argv-style arguments. Returns CompletedProcess."""
    result = subprocess.run(
        args,
        capture_output=capture,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise SystemExit(f"Error: {msg}" if msg else f"Command failed ({result.returncode})")
    return result


def inspect_container(format_string: str) -> str | None:
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


def container_status() -> str | None:
    """Return the container's Docker state ('running', 'exited', ...), or None."""
    out = inspect_container("{{.State.Status}}")
    return out.strip() if out else None


def container_running() -> bool:
    """True only when the container exists and is running."""
    return container_status() == "running"


def container_env() -> dict[str, str]:
    """The container's configured environment variables (its compose-time config)."""
    out = inspect_container("{{range .Config.Env}}{{println .}}{{end}}")
    env: dict[str, str] = {}
    for line in (out or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key] = value
    return env


def compose(
    *args: str, env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run docker compose with the vpn.yml file.

    Interpolation values come from the process environment: the project .env
    file merged with any overrides. Process env beats compose's own .env
    lookup, so no temporary env file (and no secrets on disk) is needed.
    """
    base = read_env_file(Path(COMPOSE_FILE).parent / ".env")
    merged = {**base, **(env_overrides or {})}
    cmd = ["docker", "compose", "-f", COMPOSE_FILE, *args]
    return run(*cmd, env={**os.environ, **merged})
