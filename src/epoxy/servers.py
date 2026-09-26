"""Server fetching, caching, and display."""

import json
import time
from concurrent.futures import ThreadPoolExecutor

from rich.console import Console
from rich.table import Table

from epoxy.config import (
    CACHE_DIR,
    CACHE_FILE,
    CACHE_VERSION,
    DEFAULT_PROTOCOL,
    SERVER_FETCH_TIMEOUT_S,
    cache_ttl,
    image_ref,
)
from epoxy.docker import run
from epoxy.providers import PROVIDERS, get_active_providers
from epoxy.textutil import fold

SERVER_SEP = " - "

# Printed before each provider's table so a single container boot's combined
# stdout can be split back into per-provider blocks.
_PROVIDER_MARKER = "@@PROVIDER@@"


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


#: Column positions assumed when the header row is missing or unrecognised.
_FALLBACK_COLUMNS = {"country": 1, "city": 2}


def _header_columns(lines: list[str]) -> dict[str, int]:
    """Locate the markdown header's columns, falling back to a fixed layout.

    'format-servers' is expected to print a header naming its columns, but the
    parse must survive a version that does not: without one, fall back to the
    historical positional layout rather than dropping every server.
    """
    for line in lines:
        lowered = [cell.strip().lower() for cell in line.split("|")]
        if "country" in lowered and "city" in lowered:
            return {
                name: lowered.index(name)
                for name in ("country", "city", "hostname", "vpn")
                if name in lowered
            }
    return dict(_FALLBACK_COLUMNS)


def _parse_servers_output(lines: list[str]) -> list[ServerRow]:
    """Parse the container 'format-servers' markdown output into row dicts."""
    idx = _header_columns(lines)
    country_idx = idx.get("country", _FALLBACK_COLUMNS["country"])
    city_idx = idx.get("city", _FALLBACK_COLUMNS["city"])

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
        image_ref(),
        "format-servers",
        f"-{provider}",
        capture=True,
        check=False,
        timeout=SERVER_FETCH_TIMEOUT_S,
    )
    if result.returncode != 0:
        return []
    return _parse_servers_output(result.stdout.splitlines())


def _fetch_all_servers(providers: list[str]) -> dict[str, list[ServerRow]] | None:
    """Fetch every provider's servers with a single container boot.

    format-servers accepts exactly one provider per invocation, but its data is
    embedded in the image, so the shell loops a per-provider call inside one
    `docker run` — the expensive container startup is paid once instead of once
    per provider. Returns None when the boot failed or nothing parsed.
    """
    if not providers:
        return {}
    loop = "; ".join(
        f'echo "{_PROVIDER_MARKER}{provider}"; /gluetun-entrypoint format-servers -{provider}'
        for provider in providers
    )
    result = run(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "/bin/sh",
        image_ref(),
        "-c",
        loop,
        capture=True,
        check=False,
        timeout=SERVER_FETCH_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None

    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith(_PROVIDER_MARKER):
            current = line[len(_PROVIDER_MARKER) :].strip()
            blocks.setdefault(current, [])
        elif current is not None:
            blocks[current].append(line)

    by_provider = {
        provider: _parse_servers_output(blocks.get(provider, [])) for provider in providers
    }
    return by_provider if any(by_provider.values()) else None


def _read_cache() -> dict[str, list[ServerRow]] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        if data.get("v") != CACHE_VERSION:
            return None
        servers = data["servers"]
        if not isinstance(servers, dict):
            return None
        if time.time() - data["ts"] < cache_ttl():
            return servers
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


def _write_cache(servers: dict[str, list[ServerRow]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"v": CACHE_VERSION, "ts": time.time(), "servers": servers}))


def get_servers() -> dict[str, list[ServerRow]]:
    """Fetch servers for every known provider; dict[provider, rows].

    All providers are fetched regardless of credentials so the shared cache is
    instance-independent (a cache written by one instance is never missing
    another instance's providers). Prefers one container boot for all providers
    and falls back to a parallel per-provider fetch when that fails (e.g. on
    older images). Callers narrow to credentialed pairs via ``listable_servers``.
    """
    cached = _read_cache()
    if cached is not None:
        return cached
    providers = sorted(PROVIDERS)
    by_provider = _fetch_all_servers(providers)
    if by_provider is None:
        by_provider = {}
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
