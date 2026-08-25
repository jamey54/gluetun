"""Server fetching, caching, and display."""

import json
import time
from concurrent.futures import ThreadPoolExecutor

from rich.console import Console
from rich.table import Table

from vpn.config import CACHE_DIR, CACHE_FILE, CACHE_TTL
from vpn.docker import GLUETUN_IMAGE, run
from vpn.providers import DEFAULT_PROTOCOL, get_active_providers
from vpn.textutil import fold

SERVER_SEP = " - "
CACHE_VERSION = 3


def parse_server_selection(selection: str) -> tuple[str | None, str | None, str, str | None]:
    """Parse '[provider/protocol] Country - City' into (provider, protocol, country, city)."""
    provider = protocol = None
    if selection.startswith("["):
        bracket, rest = selection.split("]", 1)
        inner = bracket[1:]
        if "/" in inner:
            provider, _, protocol = inner.partition("/")
        else:
            provider = inner
        selection = rest.strip()
    parts = selection.split(SERVER_SEP, 1)
    country = parts[0].strip()
    city = parts[1].strip() if len(parts) > 1 else None
    return provider, protocol, country, city


# ---------------------------------------------------------------------------
# Server cache
# ---------------------------------------------------------------------------


ServerRow = dict[str, str]


def _parse_servers_output(lines: list[str]) -> list[ServerRow]:
    """Parse gluetun 'format-servers' markdown output into row dicts."""
    idx: dict[str, int] = {}
    for line in lines:
        cells = [c.strip() for c in line.split("|")]
        lowered = [c.lower() for c in cells]
        if "country" in lowered and "city" in lowered:
            idx = {
                name: lowered.index(name)
                for name in ("country", "city", "hostname", "vpn")
                if name in lowered
            }
            break

    country_idx = idx.get("country")
    city_idx = idx.get("city")
    if country_idx is None or city_idx is None:
        country_idx, city_idx = 1, 2

    servers: list[ServerRow] = []
    for line in lines:
        cells = [c.strip() for c in line.split("|")]
        if len(cells) <= max(country_idx, city_idx):
            continue
        inner = cells[1:-1]
        if not inner or all(c == "" or c.startswith("-") for c in inner):
            continue
        country = cells[country_idx]
        city = cells[city_idx]
        if not country or not city or country.lower() == "country":
            continue
        vpn_cell = cells[idx["vpn"]].lower() if "vpn" in idx and idx["vpn"] < len(cells) else ""
        vpn = vpn_cell or DEFAULT_PROTOCOL
        hostname = (
            cells[idx["hostname"]].strip("`")
            if "hostname" in idx and idx["hostname"] < len(cells)
            else ""
        )
        servers.append({"country": country, "city": city, "hostname": hostname, "vpn": vpn})
    return servers


def _fetch_servers(provider: str) -> list[ServerRow]:
    result = run(
        "docker",
        "run",
        "--rm",
        GLUETUN_IMAGE,
        "format-servers",
        f"-{provider}",
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return _parse_servers_output(result.stdout.splitlines())


def _read_cache() -> dict[str, list[ServerRow]] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        if data.get("v") != CACHE_VERSION:
            return None
        if time.time() - data["ts"] < CACHE_TTL:
            servers: dict[str, list[ServerRow]] = data["servers"]
            return servers
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


def _write_cache(servers: dict[str, list[ServerRow]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"v": CACHE_VERSION, "ts": time.time(), "servers": servers}))


def get_servers() -> dict[str, list[ServerRow]]:
    """Fetch servers for all credentialed providers in parallel; dict[provider, rows]."""
    cached = _read_cache()
    if cached is not None:
        return cached
    providers = sorted({provider for provider, _ in get_active_providers()})
    by_provider: dict[str, list[ServerRow]] = {}
    with ThreadPoolExecutor(max_workers=max(len(providers), 1)) as pool:
        for provider, rows in zip(providers, pool.map(_fetch_servers, providers), strict=True):
            by_provider[provider] = rows
    if any(by_provider.values()):
        _write_cache(by_provider)
    return by_provider


def listable_servers(by_provider: dict[str, list[ServerRow]]) -> dict[str, list[ServerRow]]:
    """Keep only rows whose (provider, protocol) pair has credentials; drop empty providers."""
    active = get_active_providers()
    return {
        provider: filtered
        for provider, rows in by_provider.items()
        if (filtered := [s for s in rows if (provider, s.get("vpn", DEFAULT_PROTOCOL)) in active])
    }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def sorted_server_rows(
    by_provider: dict[str, list[ServerRow]],
) -> list[tuple[str, str, str, str, str]]:
    """Flatten rows to (provider, protocol, country, city, hostname), sorted."""
    rows: list[tuple[str, str, str, str, str]] = [
        (provider, s.get("vpn", DEFAULT_PROTOCOL), s["country"], s["city"], s.get("hostname", ""))
        for provider, srvs in by_provider.items()
        for s in srvs
    ]
    return sorted(
        rows,
        key=lambda r: (r[0], fold(r[2]), fold(r[3]), r[4]),
    )


def print_servers_table(by_provider: dict[str, list[ServerRow]]) -> None:
    """Print all servers as an aligned table: Provider | Protocol | Country | City | Server."""
    rows = sorted_server_rows(by_provider)
    console = Console(highlight=False)
    table = Table(box=None, padding=(0, 1, 0, 0), header_style="bold")
    table.add_column("Provider", style="cyan", no_wrap=True)
    table.add_column("Protocol", style="dim", no_wrap=True)
    table.add_column("Country", no_wrap=True)
    table.add_column("City", no_wrap=True)
    table.add_column("Server", style="dim", no_wrap=True)
    for provider, protocol, country, city, hostname in rows:
        table.add_row(provider, protocol, country, city, hostname or "-")
    console.print(table)
    providers = len({r[0] for r in rows})
    console.print(
        f"[dim]{len(rows)} server{'s' if len(rows) != 1 else ''}"
        f" · {providers} provider{'s' if providers != 1 else ''}[/dim]"
    )
