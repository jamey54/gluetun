"""Server fetching, caching, and display."""

import json
import time
import unicodedata
from pathlib import Path

from rich.console import Console
from rich.table import Table

from vpn.config import CACHE_TTL
from vpn.docker import GLUETUN_IMAGE, run
from vpn.providers import get_active_providers

SERVER_SEP = " - "
CACHE_VERSION = 2
CACHE_DIR = Path.home() / ".cache" / "gluetun"
CACHE_FILE = CACHE_DIR / "servers.json"


def strip_accents(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


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
# Display
# ---------------------------------------------------------------------------


def _sorted_server_rows(by_provider):
    """Flatten to (provider, country, city, hostname) rows sorted by provider, country, city, hostname."""
    rows = [
        (provider, s["country"], s["city"], s.get("hostname", ""))
        for provider, srvs in by_provider.items()
        for s in srvs
    ]
    return sorted(
        rows,
        key=lambda r: (r[0], strip_accents(r[1]).lower(), strip_accents(r[2]).lower(), r[3]),
    )


def print_servers_table(by_provider):
    """Print all servers as an aligned table: Provider | Country | City | Server."""
    rows = _sorted_server_rows(by_provider)
    console = Console(highlight=False)
    table = Table(box=None, padding=(0, 1, 0, 0), header_style="bold")
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
