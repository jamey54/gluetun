"""Configuration via environment variables."""

import os
from importlib.resources import files as resource_files
from pathlib import Path

CONTAINER = os.getenv("GLUETUN_CONTAINER", "gluetun")
CACHE_TTL = int(os.getenv("GLUETUN_CACHE_TTL", "3600"))


def _resolve_compose_file():
    """Locate vpn.yml: env override, then cwd, then the packaged copy."""
    override = os.getenv("GLUETUN_COMPOSE_FILE")
    if override:
        return override
    local = Path.cwd() / "vpn.yml"
    if local.exists():
        return str(local)
    return str(resource_files("vpn").joinpath("vpn.yml"))


COMPOSE_FILE = _resolve_compose_file()


def read_env_file(path):
    """Parse a .env-style file into a dict (skips blanks and # comments)."""
    env = {}
    path = Path(path)
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _load_dotenv():
    env_path = Path(COMPOSE_FILE).parent / ".env"
    for k, v in read_env_file(env_path).items():
        os.environ.setdefault(k, v)


_load_dotenv()
