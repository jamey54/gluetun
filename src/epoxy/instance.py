"""Per-instance identity: name, control port, env, compose file, lock, registry.

An *instance* is one VPN container fully owned by epoxy, identified by its
docker container name (== instance name). Commands resolve an instance up front
and run inside ``instance_context``; everything else reads the active instance
through ``current_instance()``.

An instance name comes from ``--instance`` or else ``EPOXY_INSTANCE``; with
neither, resolution is a usage error — there is no hidden default instance.

Isolation invariants:
- container name is always the instance name (never derived from the compose
  project, which is pinned to ``epoxy-<instance>`` via ``docker compose -p``);
- swaps/benches serialize on ``~/.cache/epoxy/locks/<instance>.lock`` per
  instance only;
- every docker exec / IP probe / status read targets the instance's container
  name explicitly.
"""

import contextlib
import json
import os
import re
import shutil
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from importlib.resources import files as resource_files
from pathlib import Path

import click

from epoxy import config
from epoxy.config import BASE_CONTROL_PORT, INSTANCE_ENV_VAR, JsonDoc, image_ref, read_env_file

INSTANCE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
PORT_RANGE = range(BASE_CONTROL_PORT, 9001)


def parse_instance_name(name: str) -> str:
    """Validate an instance name against docker-safe rules; usage error on fail."""
    name = name.strip()
    if not INSTANCE_PATTERN.match(name):
        raise click.UsageError(
            f"Invalid instance name {name!r}: must match {INSTANCE_PATTERN.pattern}"
        )
    return name


def required_name(instance: str | None) -> str:
    """An explicit instance name wins; else EPOXY_INSTANCE; else a usage error."""
    name = instance or os.getenv(INSTANCE_ENV_VAR)
    if not name:
        raise click.UsageError(f"No instance selected: pass --instance or set {INSTANCE_ENV_VAR}.")
    return parse_instance_name(name)


@dataclass(frozen=True)
class Instance:
    """A fully-resolved epoxy instance: one owned VPN container."""

    name: str
    control_port: int
    env_file: Path | None
    env: dict[str, str]
    compose_file: str

    @property
    def container(self) -> str:
        return self.name

    @property
    def project(self) -> str:
        return f"epoxy-{self.name}"

    @property
    def lock_file(self) -> str:
        return str(config.LOCKS_DIR / f"{self.name}.lock")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.control_port}"


# ---------------------------------------------------------------------------
# Registry (persisted per-instance config)
# ---------------------------------------------------------------------------


def registry_path(name: str) -> Path:
    return config.INSTANCES_DIR / f"{name}.json"


def read_registry(name: str) -> JsonDoc | None:
    """Persisted instance state (control port, env file); None when absent."""
    path = registry_path(name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_registry(instance: Instance) -> None:
    """Persist the instance's control port and env file for later reuse."""
    config.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "instance": instance.name,
        "control_port": instance.control_port,
        "env_file": str(instance.env_file) if instance.env_file else None,
    }
    registry_path(instance.name).write_text(json.dumps(data, indent=2) + "\n")


def list_registry() -> list[str]:
    """Instances known to the registry, sorted."""
    if not config.INSTANCES_DIR.is_dir():
        return []
    return sorted(p.stem for p in config.INSTANCES_DIR.iterdir() if p.suffix == ".json")


def delete_instance_state(name: str) -> None:
    """Delete persisted state: registry record, generated compose dir, lockfile.

    Never raises: missing files are skipped, and the compose dir is only
    removed when it is exactly ``INSTANCES_DIR/<name>``.
    """
    with contextlib.suppress(OSError):
        registry_path(name).unlink(missing_ok=True)
    compose_dir = Path(compose_file_for(name)).parent
    if compose_dir.name == name and compose_dir.parent == config.INSTANCES_DIR:
        shutil.rmtree(compose_dir, ignore_errors=True)
    with contextlib.suppress(OSError):
        (config.LOCKS_DIR / f"{name}.lock").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------


def build_env(env_file: Path | None) -> dict[str, str]:
    """Merged env for an instance: its env source wins over the process env.

    ``--env-file`` replaces the shared default; otherwise every instance reads
    the shared ``.env`` in the current working directory (``cp .env.sample
    .env``). The process environment is the fallback for everything a source
    doesn't set.
    """
    base = read_env_file(env_file) if env_file is not None else read_env_file(Path.cwd() / ".env")
    return {**os.environ, **base}


def env_lookup(name: str) -> str | None:
    """Effective value for a variable: the active instance's env first."""
    return current_instance().env.get(name)


# ---------------------------------------------------------------------------
# Compose files
# ---------------------------------------------------------------------------


def compose_file_for(name: str) -> str:
    """Every instance uses a generated compose file from the bundled template."""
    return str(config.INSTANCES_DIR / name / "compose.yml")


def render_compose(name: str, port: int) -> str:
    """Per-instance compose file: bundled epoxy.yml with image, name, port swapped.

    The project is pinned by docker.compose via ``-p``, so the file only pins
    the image ref, the container name (== instance) and the host control port.
    """
    body = resource_files("epoxy").joinpath("epoxy.yml").read_text()
    body = body.replace("image: EPOXY_IMAGE_REF", f"image: {image_ref()}")
    body = body.replace("container_name: epoxy", f"container_name: {name}")
    body = re.sub(r"127\.0\.0\.1:\d+:8000/tcp", f"127.0.0.1:{port}:8000/tcp", body)
    return body


def ensure_compose_file(instance: Instance) -> None:
    """Write the generated per-instance compose file."""
    path = Path(instance.compose_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_compose(instance.name, instance.control_port))


# ---------------------------------------------------------------------------
# Resolution & context
# ---------------------------------------------------------------------------


def resolve_instance(
    name: str | None, control_port: int | None = None, env_file: str | None = None
) -> Instance:
    """Build an instance, applying explicit values over persisted registry state.

    ``name`` may be None: it then resolves via ``required_name`` (env, else
    error).
    """
    name = required_name(name)
    registry = read_registry(name)

    env_file_path = Path(env_file) if env_file else None
    if env_file_path is None and registry and registry.get("env_file"):
        env_file_path = Path(str(registry["env_file"]))

    compose_file = compose_file_for(name)

    if control_port is None:
        registry_port = registry.get("control_port") if registry else None
        if isinstance(registry_port, int):
            control_port = registry_port
    if control_port is None:
        control_port = BASE_CONTROL_PORT

    return Instance(
        name=name,
        control_port=control_port,
        env_file=env_file_path,
        env=build_env(env_file_path),
        compose_file=compose_file,
    )


def default_instance() -> Instance:
    return resolve_instance(None)


_active: ContextVar[Instance | None] = ContextVar("epoxy_active_instance", default=None)


def current_instance() -> Instance:
    """The active instance, falling back to the default (recomputed each call)."""
    active = _active.get()
    return active if active is not None else default_instance()


@contextmanager
def instance_context(instance: Instance) -> Iterator[None]:
    """Run inside a specific instance scope."""
    token = _active.set(instance)
    try:
        yield
    finally:
        _active.reset(token)


# ---------------------------------------------------------------------------
# Control port allocation
# ---------------------------------------------------------------------------


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def allocate_free_port() -> int:
    """First free host port in [8000, 9000], or a scripted error."""
    for port in PORT_RANGE:
        if not _port_in_use(port):
            return port
    raise SystemExit("No free control port in [8000, 9000]; pass --ctl-port explicitly.")
