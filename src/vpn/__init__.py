"""vpn — Gluetun CLI manager."""

import json
import os
import subprocess
import tempfile
import time
import unicodedata
from importlib.resources import files as resource_files
from pathlib import Path

import click
import questionary
from questionary import Separator
from rich.console import Console
from rich.table import Table

# ---------------------------------------------------------------------------
# Config (env vars — override these to customize behavior)
# ---------------------------------------------------------------------------

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
        "required_env": ["PROTONVPN_WIREGUARD_PRIVATE_KEY", "PROTONVPN_WIREGUARD_ADDRESSES"],
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
CACHE_VERSION = 2
CACHE_DIR = Path.home() / ".cache" / "gluetun"
CACHE_FILE = CACHE_DIR / "servers.json"
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
    env_path = Path(COMPOSE_FILE).parent / ".env"
    merged = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                merged[k.strip()] = v.strip()
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


def _strip_accents(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


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
    prev_city = None
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
                if actual and _strip_accents(actual).lower() == _strip_accents(expected_city).lower():
                    return info
                if prev_city is not None and actual != prev_city:
                    click.echo(f"Location changed to {actual}, VPN is connected.")
                    return info
                prev_city = actual
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

    country_idx = city_idx = vpn_idx = hostname_idx = None
    for line in lines:
        cells = line.split("|")
        for i, cell in enumerate(cells):
            text = cell.strip().lower()
            if text == "country" and country_idx is None:
                country_idx = i
            elif text == "city" and city_idx is None:
                city_idx = i
            elif text == "vpn" and vpn_idx is None:
                vpn_idx = i
            elif text == "hostname" and hostname_idx is None:
                hostname_idx = i
        if country_idx is not None and city_idx is not None:
            break

    if country_idx is None or city_idx is None:
        country_idx, city_idx = 1, 2

    servers = []
    for line in lines:
        cells = line.split("|")
        if len(cells) <= max(country_idx, city_idx):
            continue
        inner = [c.strip() for c in cells[1:-1]]
        if not inner or all(c == "" or c.startswith("-") for c in inner):
            continue
        if vpn_idx is not None and vpn_idx < len(cells):
            vpn_type = cells[vpn_idx].strip().lower()
            if vpn_type != "wireguard":
                continue
        country = cells[country_idx].strip()
        city = cells[city_idx].strip()
        hostname = cells[hostname_idx].strip().strip("`") if hostname_idx is not None and hostname_idx < len(cells) else ""
        if country and city and country.lower() != "country":
            servers.append({"country": country, "city": city, "hostname": hostname})
    return servers


def _read_cache():
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        if data.get("v") != CACHE_VERSION:
            return None
        if time.time() - data.get("ts", 0) < CACHE_TTL:
            return data["servers"]
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _write_cache(servers):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"v": CACHE_VERSION, "ts": time.time(), "servers": servers}))


def get_servers():
    """Fetch servers for all active providers. Returns dict[provider, list[dict]]."""
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
# Server listing
# ---------------------------------------------------------------------------


def _sorted_server_rows(by_provider):
    """Flatten to (provider, country, city, hostname) rows sorted by country, city, provider."""
    rows = [
        (provider, s["country"], s["city"], s.get("hostname", ""))
        for provider, srvs in by_provider.items()
        for s in srvs
    ]
    return sorted(
        rows,
        key=lambda r: (_strip_accents(r[1]).lower(), _strip_accents(r[2]).lower(), r[0]),
    )


def print_servers_table(by_provider):
    """Print all servers as an aligned table: Provider | Country | City | Server."""
    rows = _sorted_server_rows(by_provider)
    console = Console(highlight=False)
    table = Table(box=None, padding=(0, 2, 0, 0), header_style="bold")
    table.add_column("Provider", style="cyan", no_wrap=True)
    table.add_column("Country", no_wrap=True)
    table.add_column("City", no_wrap=True)
    table.add_column("Server", style="dim", no_wrap=True)
    for provider, country, city, hostname in rows:
        table.add_row(provider, country, city, hostname or "-")
    console.print(table)
    providers = len({r[0] for r in rows})
    console.print(
        f"[dim]{len(rows)} server{'s' if len(rows) != 1 else ''}"
        f" · {providers} provider{'s' if providers != 1 else ''}[/dim]"
    )


# ---------------------------------------------------------------------------
# Interactive selection
# ---------------------------------------------------------------------------


def select_server(by_provider, prompt="Select server: "):
    """Interactive server selection with provider grouping."""
    from questionary.prompts.common import InquirerControl

    _orig_filtered = InquirerControl.filtered_choices.fget

    @property
    def _filtered_with_separators(self):
        if not self.search_filter:
            return self.choices
        filtered = [
            c for c in self.choices
            if isinstance(c, Separator)
            or self.search_filter.lower() in c.title.lower()
        ]
        self.found_in_search = any(
            not isinstance(c, Separator) for c in filtered
        )
        return filtered if self.found_in_search else self.choices

    InquirerControl.filtered_choices = _filtered_with_separators

    choices = []
    rows = _sorted_server_rows(by_provider)
    widths = {
        key: max(len(r[i]) for r in rows)
        for i, key in enumerate(("provider", "country", "city"))
    }
    for provider, country, city, hostname in rows:
        title = (
            f"{provider:<{widths['provider']}}  "
            f"{country:<{widths['country']}}  "
            f"{city:<{widths['city']}}  "
            f"{hostname or '-'}"
        )
        value = f"[{provider}] {country}{SERVER_SEP}{city}"
        choices.append(questionary.Choice(title=title, value=value))

    try:
        return questionary.select(
            message=prompt,
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
        ).ask()
    finally:
        InquirerControl.filtered_choices = _orig_filtered


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


DEBUG = False


@click.group()
@click.option("--debug", is_flag=True, envvar="VPN_DEBUG", help="Enable debug output")
def cli(debug):
    """Gluetun VPN manager."""
    global DEBUG
    DEBUG = debug


@cli.command()
@click.option("--provider", required=True, help="VPN provider (e.g. surfshark, protonvpn)")
def up(provider):
    """Start the VPN container."""
    provider = validate_provider(provider)
    overrides = get_provider_env(provider)
    if DEBUG:
        click.echo(f"Env: {' '.join(f'{k}={v}' for k, v in overrides.items())}")
    compose("up", "-d", env_overrides=overrides)
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
    overrides = get_provider_env(provider)
    if DEBUG:
        click.echo(f"Env: {' '.join(f'{k}={v}' for k, v in overrides.items())}")
    compose("up", "-d", "--force-recreate", env_overrides=overrides)
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
    print_servers_table(by_provider)


@cli.command()
def server():
    """Interactively select a server and restart."""
    by_provider = get_servers()
    if not any(by_provider.values()):
        raise SystemExit("No servers found. Is Docker running?")

    selection = select_server(by_provider)
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
    if DEBUG:
        click.echo(f"Env: {' '.join(f'{k}={v}' for k, v in overrides.items())}")
    compose("up", "-d", env_overrides=overrides)

    click.echo(f"VPN restarted ({provider}) → {country}" + (f" / {city}" if city else ""))
    print_ip_status(expected_city=city)


if __name__ == "__main__":
    cli()
