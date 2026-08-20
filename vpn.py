#!/usr/bin/env python3
"""vpn — Gluetun CLI manager."""

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# Config (env vars — override these to customize behavior)
# ---------------------------------------------------------------------------

CONTAINER = os.getenv("GLUETUN_CONTAINER", "gluetun")
COMPOSE_FILE = os.getenv(
    "GLUETUN_COMPOSE_FILE", str(Path(__file__).resolve().parent / "vpn.yml")
)
PROVIDER = os.getenv("GLUETUN_PROVIDER", "surfshark")
CACHE_TTL = int(os.getenv("GLUETUN_CACHE_TTL", "3600"))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GLUETUN_IMAGE = "qmcgaw/gluetun:latest"
IP_INFO_URL = "https://ipinfo.io"
SERVER_SEP = " - "
FZF_HEIGHT = "40%"
CACHE_FILE = Path(tempfile.gettempdir()) / "gluetun-servers.json"
IP_FETCH_RETRIES = 10
IP_FETCH_DELAY = 3

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    cmd = ["docker", "compose", "-f", COMPOSE_FILE, *args]
    if env_overrides:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            for k, v in env_overrides.items():
                f.write(f"{k}={v}\n")
            f.flush()
            try:
                cmd = ["docker", "compose", "-f", COMPOSE_FILE, "--env-file", f.name, *args]
                return run(*cmd)
            finally:
                os.unlink(f.name)
    return run(*cmd)


# ---------------------------------------------------------------------------
# IP info
# ---------------------------------------------------------------------------


def fetch_ip_info(retries=IP_FETCH_RETRIES, delay=IP_FETCH_DELAY):
    """Fetch public IP info with retries. Returns parsed dict or None."""
    for attempt in range(retries):
        result = run(
            "docker", "exec", CONTAINER, "wget", "-qO-", IP_INFO_URL,
            capture=True, check=False,
        )
        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                pass
        if attempt < retries - 1:
            click.echo(f"Waiting for VPN connection... ({attempt + 1}/{retries})")
            time.sleep(delay)
    return None


def print_ip_status(expected_country=None, expected_city=None):
    """Fetch and display IP info. Warns if location doesn't match."""
    info = fetch_ip_info()
    if not info:
        click.echo("Could not fetch public IP.")
        return
    click.echo(f"IP:       {info.get('ip', '?')}")
    click.echo(f"Location: {info.get('city', '?')}, {info.get('country', '?')}")
    click.echo(f"Org:      {info.get('org', '?')}")

    if expected_city:
        actual_city = info.get("city", "")
        if actual_city and actual_city.lower() != expected_city.lower():
            click.echo(f"Warning: Expected city '{expected_city}', got '{actual_city}'")


# ---------------------------------------------------------------------------
# Server cache
# ---------------------------------------------------------------------------


def _fetch_servers():
    result = run(
        "docker", "run", "--rm", GLUETUN_IMAGE,
        "format-servers", f"-{PROVIDER}",
        capture=True, check=False,
    )
    if result.returncode != 0:
        return []
    servers = []
    for line in result.stdout.splitlines():
        if not line.strip().startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|")]
        cols = [c for c in cols if c]
        if len(cols) < 3:
            continue
        if cols[0] in ("Region", "---", ""):
            continue
        country = cols[1]
        city = cols[2]
        servers.append(f"{country}{SERVER_SEP}{city}")
    return servers


def _read_cache():
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        if time.time() - data.get("ts", 0) < CACHE_TTL:
            return data["servers"]
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _write_cache(servers):
    CACHE_FILE.write_text(json.dumps({"ts": time.time(), "servers": servers}))


def get_servers():
    servers = _read_cache()
    if servers is not None:
        return servers
    servers = _fetch_servers()
    if servers:
        _write_cache(servers)
    return servers


# ---------------------------------------------------------------------------
# fzf
# ---------------------------------------------------------------------------


def fzf_select(items, prompt="> "):
    """Pipe items to fzf. Returns selected string or None."""
    if not shutil.which("fzf"):
        raise SystemExit(
            "fzf is not installed.\n"
            "  sudo apt install fzf\n"
            "  or: git clone --depth 1 https://github.com/junegunn/fzf ~/.fzf && ~/.fzf/install"
        )
    proc = subprocess.run(
        ["fzf", "--prompt", prompt, "--height", FZF_HEIGHT, "--reverse", "--track"],
        input="\n".join(items),
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli():
    """Gluetun VPN manager."""


@cli.command()
def up():
    """Start the VPN container."""
    compose("up", "-d")
    click.echo("VPN started.")
    print_ip_status()


@cli.command()
def down():
    """Stop the VPN container."""
    compose("down")
    click.echo("VPN stopped.")


@cli.command()
def restart():
    """Restart the VPN container."""
    compose("restart")
    click.echo("VPN restarted.")
    print_ip_status()


@cli.command()
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
@click.option("-n", "--tail", default="50", help="Number of lines to show")
def logs(follow, tail):
    """Show container logs."""
    args = ["logs"]
    if follow:
        args.append("-f")
    args.extend(["--tail", tail, CONTAINER])
    compose(*args)


@cli.command()
def update():
    """Pull latest gluetun image and recreate the container."""
    run("docker", "pull", GLUETUN_IMAGE)
    compose("up", "-d", "--force-recreate")
    click.echo("Updated and restarted.")
    print_ip_status()


@cli.command()
def ip():
    """Show the current public VPN IP."""
    print_ip_status()


@cli.command()
def status():
    """Show container status and public IP."""
    result = run(
        "docker", "inspect", "--format", "{{.State.Status}}", CONTAINER,
        capture=True, check=False,
    )
    if result.returncode != 0:
        click.echo(f"Container '{CONTAINER}' not found.")
        return
    click.echo(f"Container: {CONTAINER} ({result.stdout.strip()})")
    print_ip_status()


@cli.command()
def servers():
    """List available servers."""
    srvs = get_servers()
    if not srvs:
        raise SystemExit("No servers found. Is Docker running?")
    click.echo(f"{len(srvs)} servers available:\n")
    for s in srvs:
        click.echo(f"  {s}")


@cli.command()
def server():
    """Interactively select a server and restart."""
    srvs = get_servers()
    if not srvs:
        raise SystemExit("No servers found. Is Docker running?")

    selection = fzf_select(srvs, prompt="Select server: ")
    if not selection:
        raise SystemExit("No selection.")

    parts = selection.split(SERVER_SEP, 1)
    country = parts[0].strip()
    city = parts[1].strip() if len(parts) > 1 else None

    click.echo(f"Location: {country}" + (f" / {city}" if city else ""))

    compose("down")

    overrides = {"SERVER_COUNTRIES": country}
    if city:
        overrides["SERVER_CITIES"] = city
    compose("up", "-d", env_overrides=overrides)

    click.echo(f"VPN restarted → {country}" + (f" / {city}" if city else ""))
    print_ip_status(expected_country=country, expected_city=city)


if __name__ == "__main__":
    cli()
