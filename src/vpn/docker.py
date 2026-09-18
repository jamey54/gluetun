"""Docker / docker compose helpers."""

import contextlib
import json
import os
import subprocess
import sys

from vpn.config import CONTAINER_OP_TIMEOUT_S
from vpn.instance import current_instance

GLUETUN_IMAGE = "qmcgaw/gluetun:latest"


def run(
    *args: str,
    capture: bool = False,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command with argv-style arguments. Returns CompletedProcess."""
    try:
        result = subprocess.run(
            args,
            capture_output=capture,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        msg = f"Command timed out after {timeout:g}s"
        if check:
            raise SystemExit(f"Error: {msg}") from None
        return subprocess.CompletedProcess(tuple(args), 124, stdout="", stderr=msg)
    except OSError as exc:
        msg = f"Cannot run {args[0] if args else 'command'}: {exc}"
        if check:
            print(msg, file=sys.stderr)
            raise SystemExit(127) from None
        return subprocess.CompletedProcess(tuple(args), 127, stdout="", stderr=msg)
    if check and result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise SystemExit(f"Error: {msg}" if msg else f"Command failed ({result.returncode})")
    return result


def inspect_container(format_string: str, name: str | None = None) -> str | None:
    """Inspect the active instance's container with a Go template. None if absent."""
    container = name or current_instance().container
    try:
        result = run(
            "docker",
            "inspect",
            "--format",
            format_string,
            container,
            capture=True,
            check=False,
            timeout=CONTAINER_OP_TIMEOUT_S,
        )
    except OSError:
        return None  # docker unavailable: treat as absent for read-only probes
    return result.stdout if result.returncode == 0 else None


def container_status(name: str | None = None) -> str | None:
    """Return the container's Docker state ('running', 'exited', ...), or None."""
    out = inspect_container("{{.State.Status}}", name=name)
    return out.strip() if out else None


def container_running(name: str | None = None) -> bool:
    """True only when the container exists and is running."""
    return container_status(name=name) == "running"


def container_env() -> dict[str, str]:
    """The container's configured environment variables (its compose-time config)."""
    out = inspect_container("{{range .Config.Env}}{{println .}}{{end}}")
    env: dict[str, str] = {}
    for line in (out or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key] = value
    return env


def container_image(name: str | None = None) -> str | None:
    """The container's image reference, or None when absent."""
    out = inspect_container("{{.Config.Image}}", name=name)
    return out.strip() if out else None


def container_control_port(name: str | None = None) -> int | None:
    """Host port published for the container's control server (8000/tcp), if any."""
    out = inspect_container("{{json .NetworkSettings.Ports}}", name=name)
    if not out:
        return None
    try:
        ports = json.loads(out)
    except json.JSONDecodeError:
        return None
    bindings = ports.get("8000/tcp") or []
    if not bindings or not bindings[0].get("HostPort"):
        return None
    try:
        return int(bindings[0]["HostPort"])
    except (TypeError, ValueError):
        return None


def compose(
    *args: str,
    env_overrides: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run docker compose against the active instance's file and project.

    The project name is always pinned with ``-p`` so it never depends on the
    file location or a ``name:`` key. Interpolation env is the instance's
    merged env (its .env/env-file wins over the process environment) overlaid
    by any overrides baked at create time — no temporary env file (and no
    secrets on disk) is needed.
    """
    inst = current_instance()
    cmd = ["docker", "compose", "-f", inst.compose_file, "-p", inst.project, *args]
    env = {**inst.env, **(env_overrides or {})}
    return run(*cmd, env={**os.environ, **env}, timeout=timeout)


def launch_container(name: str, env: dict[str, str]) -> bool:
    """Start a detached one-off tun container; returns True when it launched."""
    args = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--cap-add",
        "NET_ADMIN",
        "--device",
        "/dev/net/tun:/dev/net/tun",
    ]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args.append(GLUETUN_IMAGE)
    try:
        result = run(
            *args, capture=True, check=False, timeout=CONTAINER_OP_TIMEOUT_S
        )
    except OSError:
        return False  # docker unavailable: treat as a failed launch
    return result.returncode == 0


def remove_container(name: str) -> None:
    """Force-remove a container (best effort, never raises)."""
    with contextlib.suppress(OSError):
        run("docker", "rm", "-f", name, capture=True, check=False, timeout=CONTAINER_OP_TIMEOUT_S)
