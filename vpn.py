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
CACHE_TTL = int(os.getenv("GLUETUN_CACHE_TTL", "3600"))


def _load_dotenv():
    env_path = Path(COMPOSE_FILE).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDERS = {
    "surfshark": {
        "required_env": ["SURFSHARK_WIREGUARD_PRIVATE_KEY"],
        "env_map": {
            "WIREGUARD_PRIVATE_KEY": "SURFSHARK_WIREGUARD_PRIVATE_KEY",
            "WIREGUARD_ADDRESSES": "SURFSHARK_WIREGUARD_ADDRESSES",
        },
    },
    "protonvpn": {
        "required_env": ["PROTONVPN_WIREGUARD_PRIVATE_KEY"],
        "env_map": {
            "WIREGUARD_PRIVATE_KEY": "PROTONVPN_WIREGUARD_PRIVATE_KEY",
            "WIREGUARD_ADDRESSES": "PROTONVPN_WIREGUARD_ADDRESSES",
        },
    },
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GLUETUN_IMAGE = "qmcgaw/gluetun:latest"
IP_INFO_URL = "https://ipinfo.io"
SERVER_SEP = " - "
FZF_HEIGHT = "40%"
CACHE_FILE = Path(tempfile.gettempdir()) / "gluetun-servers.json"
IP_FETCH_RETRIES = 15
IP_FETCH_DELAY = 2

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
    if not env_overrides:
        return run(*cmd)
    env_path = Path(COMPOSE_FILE).parent / ".env"
    merged = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                merged[k.strip()] = v.strip()
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


# ---------------------------------------------------------------------------
# Provider helpers
# ---------------------------------------------------------------------------


def validate_provider(name):
    """Validate provider name and check required env vars. Returns lowercase name."""
    name = name.lower()
    if name not in PROVIDERS:
        valid = ", ".join(sorted(PROVIDERS))
        raise SystemExit(f"Unknown provider '{name}'. Available: {valid}")
    missing = [v for v in PROVIDERS[name]["required_env"] if not os.getenv(v)]
    if missing:
        raise SystemExit(f"Missing env vars for {name}: {', '.join(missing)}")
    return name


def get_active_providers():
    """Return providers whose required env vars are all set."""
    return {
        name: cfg
        for name, cfg in PROVIDERS.items()
        if all(os.getenv(v) for v in cfg["required_env"])
    }


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


def parse_server_selection(selection):
    """Parse '[provider] Country / City' into (provider, country, city)."""
    provider = None
    if selection.startswith("["):
        bracket, rest = selection.split("]", 1)
        provider = bracket[1:]
        selection = rest.strip()
    parts = selection.split(SERVER_SEP, 1)
    country = parts[0].strip()
    city = parts[1].strip() if len(parts) > 1 else None
    return provider, country, city


def get_provider_env(provider):
    """Map provider-specific env vars to Gluetun's generic env vars."""
    overrides = {"VPN_SERVICE_PROVIDER": provider}
    for gluetun_var, provider_var in PROVIDERS[provider]["env_map"].items():
        value = os.getenv(provider_var)
        if value:
            overrides[gluetun_var] = value
    return overrides


# ---------------------------------------------------------------------------
# IP info
# ---------------------------------------------------------------------------


def fetch_ip_info(retries=IP_FETCH_RETRIES, delay=IP_FETCH_DELAY, expected_city=None):
    """Fetch public IP info with retries. If expected_city is set, retries until it matches."""
    for attempt in range(retries):
        result = run(
            "docker", "exec", CONTAINER, "wget", "-qO-", IP_INFO_URL,
            capture=True, check=False,
        )
        if result.returncode == 0:
            try:
                info = json.loads(result.stdout)
                if not expected_city:
                    return info
                actual = info.get("city", "")
                if actual and actual.lower() == expected_city.lower():
                    return info
                click.echo(f"Connected to {actual or '?'}, waiting for {expected_city}... ({attempt + 1}/{retries})")
            except json.JSONDecodeError:
                pass
        elif attempt < retries - 1:
            click.echo(f"Waiting for VPN connection... ({attempt + 1}/{retries})")
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def print_ip_status(expected_city=None):
    """Fetch and display IP info."""
    info = fetch_ip_info(expected_city=expected_city)
    if not info:
        click.echo("Could not fetch public IP.")
        return False
    click.echo(f"IP:       {info.get('ip', '?')}")
    click.echo(f"Location: {info.get('city', '?')}, {info.get('country', '?')}")
    click.echo(f"Org:      {info.get('org', '?')}")
    return True


# ---------------------------------------------------------------------------
# Server cache
# ---------------------------------------------------------------------------


def _fetch_servers(provider):
    result = run(
        "docker", "run", "--rm", GLUETUN_IMAGE,
        "format-servers", f"-{provider}",
        capture=True, check=False,
    )
    if result.returncode != 0:
        return []
    lines = result.stdout.splitlines()

    country_idx = city_idx = None
    for line in lines:
        if not line.strip().startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|") if c.strip()]
        for i, col in enumerate(cols):
            lower = col.lower()
            if lower == "country":
                country_idx = i
            elif lower == "city":
                city_idx = i
        if country_idx is not None and city_idx is not None:
            break

    if country_idx is None or city_idx is None:
        country_idx, city_idx = 1, 2

    servers = []
    for line in lines:
        if not line.strip().startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|") if c.strip()]
        if len(cols) <= max(country_idx, city_idx):
            continue
        if cols[0] in ("---", "") or cols[0].lower() in ("region", "country", "city"):
            continue
        country = cols[country_idx]
        city = cols[city_idx]
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
    """Fetch servers for all active providers. Returns dict[provider, list[str]]."""
    cached = _read_cache()
    if cached is not None:
        return cached
    active = get_active_providers()
    by_provider = {}
    for provider in active:
        by_provider[provider] = _fetch_servers(provider)
    if any(by_provider.values()):
        _write_cache(by_provider)
    return by_provider


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
        ["fzf", "--prompt", prompt, "--height", FZF_HEIGHT, "--reverse", "--bind", "change:first"],
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
@click.option("--provider", required=True, help="VPN provider (e.g. surfshark, protonvpn)")
def up(provider):
    """Start the VPN container."""
    provider = validate_provider(provider)
    compose("up", "-d", env_overrides=get_provider_env(provider))
    click.echo(f"VPN started ({provider}).")
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
    provider = get_current_provider()
    if not provider:
        raise SystemExit("No running container. Use 'vpn up --provider <name>' first.")
    run("docker", "pull", GLUETUN_IMAGE)
    compose("up", "-d", "--force-recreate", env_overrides=get_provider_env(provider))
    click.echo(f"Updated and restarted ({provider}).")
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
    """List available servers for all active providers."""
    by_provider = get_servers()
    if not any(by_provider.values()):
        raise SystemExit("No servers found. Is Docker running?")
    for provider, srvs in by_provider.items():
        if srvs:
            click.echo(f"\n--- {provider} ({len(srvs)} servers) ---")
            for s in srvs:
                click.echo(f"  {s}")


@cli.command()
def server():
    """Interactively select a server and restart."""
    by_provider = get_servers()
    if not any(by_provider.values()):
        raise SystemExit("No servers found. Is Docker running?")

    items = []
    for provider, srvs in by_provider.items():
        for s in srvs:
            items.append(f"[{provider}] {s}")

    selection = fzf_select(items, prompt="Select server: ")
    if not selection:
        raise SystemExit("No selection.")

    provider, country, city = parse_server_selection(selection)
    provider = validate_provider(provider)

    click.echo(f"Provider: {provider}")
    click.echo(f"Location: {country}" + (f" / {city}" if city else ""))

    compose("down")

    overrides = get_provider_env(provider)
    overrides["SERVER_COUNTRIES"] = country
    if city:
        overrides["SERVER_CITIES"] = city
    compose("up", "-d", env_overrides=overrides)

    click.echo(f"VPN restarted ({provider}) → {country}" + (f" / {city}" if city else ""))
    print_ip_status(expected_city=city)


if __name__ == "__main__":
    cli()
