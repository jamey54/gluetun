"""Docker / docker compose helpers."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from vpn.config import COMPOSE_FILE, CONTAINER, read_env_file

GLUETUN_IMAGE = "qmcgaw/gluetun:latest"


def env_lookup(name: str) -> str | None:
    """Effective value for compose substitution: process environment wins over .env file."""
    value = os.environ.get(name)
    if value is not None:
        return value
    return read_env_file(Path(COMPOSE_FILE).parent / ".env").get(name)


@dataclass(frozen=True)
class CurrentVpn:
    """Configuration read back from the running container."""

    provider: str
    protocol: str | None = None
    countries: str | None = None
    cities: str | None = None

    def location_overrides(self) -> dict[str, str]:
        """SERVER_COUNTRIES/SERVER_CITIES overrides, omitting unset ones."""
        overrides: dict[str, str] = {}
        if self.countries:
            overrides["SERVER_COUNTRIES"] = self.countries
        if self.cities:
            overrides["SERVER_CITIES"] = self.cities
        return overrides


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


def get_current_vpn() -> CurrentVpn | None:
    """Read provider, protocol and location from the running container, or None."""
    out = inspect_container("{{range .Config.Env}}{{println .}}{{end}}")
    if not out:
        return None
    values: dict[str, str | None] = {}
    wanted = ("VPN_SERVICE_PROVIDER", "VPN_TYPE", "SERVER_COUNTRIES", "SERVER_CITIES")
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in wanted:
            values[key] = value or None
    provider = values.get("VPN_SERVICE_PROVIDER")
    if not provider:
        return None
    return CurrentVpn(
        provider=provider,
        protocol=values.get("VPN_TYPE"),
        countries=values.get("SERVER_COUNTRIES"),
        cities=values.get("SERVER_CITIES"),
    )
