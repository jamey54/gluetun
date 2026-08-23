"""Docker / docker compose helpers."""

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vpn.config import COMPOSE_FILE, CONTAINER, read_env_file

GLUETUN_IMAGE = "qmcgaw/gluetun:latest"


@dataclass(frozen=True)
class CurrentVpn:
    """Configuration read back from the running container."""

    provider: str
    protocol: str | None = None
    countries: str | None = None
    cities: str | None = None

    def location_overrides(self):
        """SERVER_COUNTRIES/SERVER_CITIES overrides, omitting unset ones."""
        overrides = {}
        if self.countries:
            overrides["SERVER_COUNTRIES"] = self.countries
        if self.cities:
            overrides["SERVER_CITIES"] = self.cities
        return overrides


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
    """Read provider, protocol and location from the running container, or None."""
    out = inspect_container("{{range .Config.Env}}{{println .}}{{end}}")
    if not out:
        return None
    values = {}
    wanted = ("VPN_SERVICE_PROVIDER", "VPN_TYPE", "SERVER_COUNTRIES", "SERVER_CITIES")
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in wanted:
            values[key] = value or None
    if not values.get("VPN_SERVICE_PROVIDER"):
        return None
    return CurrentVpn(
        provider=values["VPN_SERVICE_PROVIDER"],
        protocol=values.get("VPN_TYPE"),
        countries=values.get("SERVER_COUNTRIES"),
        cities=values.get("SERVER_CITIES"),
    )
