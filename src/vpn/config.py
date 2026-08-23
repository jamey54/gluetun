"""Configuration via environment variables."""

import os
from importlib.resources import files as resource_files
from pathlib import Path

CONTAINER: str = os.getenv("GLUETUN_CONTAINER", "gluetun")
CACHE_TTL: int = int(os.getenv("GLUETUN_CACHE_TTL", "3600"))


def _resolve_compose_file() -> str:
    """Locate vpn.yml: env override, then cwd, then the packaged copy."""
    override = os.getenv("GLUETUN_COMPOSE_FILE")
    if override:
        return override
    local = Path.cwd() / "vpn.yml"
    if local.exists():
        return str(local)
    return str(resource_files("vpn").joinpath("vpn.yml"))


COMPOSE_FILE: str = _resolve_compose_file()


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


def _load_dotenv() -> None:
    env_path = Path(COMPOSE_FILE).parent / ".env"
    for key, value in read_env_file(env_path).items():
        os.environ.setdefault(key, value)


_load_dotenv()
