#!/usr/bin/env python3
"""vpn — Gluetun CLI manager."""

import json
import os
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path

import click
import questionary
from questionary import Separator

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


COUNTRY_CODES = {
    "Albania": "AL", "Algeria": "DZ", "Argentina": "AR", "Armenia": "AM",
    "Australia": "AU", "Austria": "AT", "Azerbaijan": "AZ", "Bahamas": "BS",
    "Bahrain": "BH", "Bangladesh": "BD", "Belarus": "BY", "Belgium": "BE",
    "Bolivia": "BO", "Bosnia and Herzegovina": "BA", "Brazil": "BR",
    "Bulgaria": "BG", "Cambodia": "KH", "Canada": "CA", "Chile": "CL",
    "Colombia": "CO", "Costa Rica": "CR", "Croatia": "HR", "Cyprus": "CY",
    "Czech Republic": "CZ", "Czechia": "CZ", "Denmark": "DK", "Ecuador": "EC",
    "Estonia": "EE", "Finland": "FI", "France": "FR", "Georgia": "GE",
    "Germany": "DE", "Ghana": "GH", "Greece": "GR", "Guatemala": "GT",
    "Honduras": "HN", "Hong Kong": "HK", "Hungary": "HU", "Iceland": "IS",
    "India": "IN", "Indonesia": "ID", "Ireland": "IE", "Israel": "IL",
    "Italy": "IT", "Jamaica": "JM", "Japan": "JP", "Jordan": "JO",
    "Kazakhstan": "KZ", "Kenya": "KE", "Kuwait": "KW", "Latvia": "LV",
    "Lithuania": "LT", "Luxembourg": "LU", "Malaysia": "MY", "Malta": "MT",
    "Mexico": "MX", "Moldova": "MD", "Monaco": "MC", "Mongolia": "MN",
    "Montenegro": "ME", "Morocco": "MA", "Myanmar": "MM", "Nepal": "NP",
    "Netherlands": "NL", "New Zealand": "NZ", "Nigeria": "NG",
    "North Macedonia": "MK", "Norway": "NO", "Pakistan": "PK", "Panama": "PA",
    "Paraguay": "PY", "Peru": "PE", "Philippines": "PH", "Poland": "PL",
    "Portugal": "PT", "Romania": "RO", "Russia": "RU", "Saudi Arabia": "SA",
    "Serbia": "RS", "Singapore": "SG", "Slovakia": "SK", "Slovenia": "SI",
    "South Africa": "ZA", "South Korea": "KR", "Spain": "ES", "Sri Lanka": "LK",
    "Sweden": "SE", "Switzerland": "CH", "Taiwan": "TW", "Thailand": "TH",
    "Turkey": "TR", "UAE": "AE", "United Arab Emirates": "AE",
    "Ukraine": "UA", "United Kingdom": "GB", "United States": "US",
    "Uruguay": "UY", "Uzbekistan": "UZ", "Venezuela": "VE", "Vietnam": "VN",
}


def _country_flag(country):
    code = COUNTRY_CODES.get(country)
    if not code or len(code) != 2:
        return ""
    return chr(0x1F1E6 + ord(code[0]) - ord("A")) + chr(0x1F1E6 + ord(code[1]) - ord("A")) + " "


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
# Interactive selection
# ---------------------------------------------------------------------------


def select_server(by_provider, prompt="Select server: "):
    """Interactive server selection with provider grouping."""

    def _search_matcher(search_filter, choice):
        if isinstance(choice, Separator):
            return True
        title = choice.title if isinstance(choice.title, str) else " ".join(
            frag[1] for frag in choice.title
        )
        return search_filter.lower() in title.lower()

    choices = []
    for provider, srvs in by_provider.items():
        if not srvs:
            continue
        choices.append(Separator(f"  {provider.upper()}"))
        for s in srvs:
            flag = _country_flag(s["country"])
            title = f"{flag}{s['country']} / {s['city']}"
            if s["hostname"]:
                title += f"  ({s['hostname']})"
            value = f"[{provider}] {s['country']}{SERVER_SEP}{s['city']}"
            choices.append(questionary.Choice(title=title, value=value))

    return questionary.select(
        message=prompt,
        choices=choices,
        use_search_filter=True,
        use_jk_keys=False,
        search_matcher=_search_matcher,
    ).ask()


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
    for provider, srvs in by_provider.items():
        if srvs:
            click.echo(f"\n--- {provider} ({len(srvs)} servers) ---")
            for s in srvs:
                host = f"  ({s['hostname']})" if s.get("hostname") else ""
                click.echo(f"  {s['country']}{SERVER_SEP}{s['city']}{host}")


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
