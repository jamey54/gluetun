"""Public IP probing, leak detection, and connection verification.

Verification is leak-first: an observation counts as "connected" only when the
observed exit IP differs from the host's bare public IP. Country matching is
advisory — a VPN exit that geolocates elsewhere (virtual locations) is a
warning, not a failure.

Probability of getting a public IP, the upstream way: instead of one echo service
(ipinfo.io) whose rate limit stalls the retry loop, probe the same services
the container image uses, in parallel, and accept the most-agreed answer. One provider
being rate-limited (HTTP 429) no longer blocks everyone else.
"""

import json
import os
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.request import urlopen

import click

from epoxy.config import (
    CURRENT_EXIT_IP_RETRIES,
    IP_FETCH_DELAY,
    IP_FETCH_RETRIES,
    IP_INFO_URL,
    PROBE_EXEC_TIMEOUT_S,
    PROBE_TIMEOUT,
    REAL_IP_ENV_VAR,
    REAL_IP_TIMEOUT_S,
    JsonDoc,
)
from epoxy.countries import COUNTRY_NAMES, resolve_country, to_code
from epoxy.docker import run
from epoxy.instance import current_instance
from epoxy.statusdoc import classify_verdict
from epoxy.textutil import fold

_real_ip_cache: str | None = None
_real_ip_info: JsonDoc | None = None


@dataclass
class IpResult:
    """An accepted public-IP observation (not excluded as bare/unchanged)."""

    info: JsonDoc
    matched: bool  # country matches expected_country; always True when none given
    sources: tuple[str, ...] = ()  # provider(s) that supplied the IP


@dataclass
class IpOutcome:
    """Outcome of a verification poll."""

    result: IpResult | None = None  # None = never observed an acceptable IP
    last_info: JsonDoc | None = None  # last raw observation, accepted or not


def real_ip() -> str | None:
    """The host's bare public IP, cached per process. None if it can't be fetched."""
    global _real_ip_cache
    override = os.getenv(REAL_IP_ENV_VAR)
    if override:
        return override
    if _real_ip_cache is not None:
        return _real_ip_cache
    _fetch_real_ip_info()
    return _real_ip_cache


def real_ip_info() -> JsonDoc | None:
    """Full ipinfo.io response for the host's bare IP. None if unreachable."""
    if _real_ip_info is not None:
        return _real_ip_info
    _fetch_real_ip_info()
    return _real_ip_info


def _fetch_real_ip_info() -> None:
    """Fetch and cache the full host IP response from ipinfo.io."""
    global _real_ip_cache, _real_ip_info
    override = os.getenv(REAL_IP_ENV_VAR)
    if override:
        _real_ip_cache = override
        return
    try:
        with urlopen(IP_INFO_URL, timeout=REAL_IP_TIMEOUT_S) as response:
            data = json.loads(response.read().decode(errors="replace"))
        if isinstance(data, dict) and data:
            _real_ip_info = data
            ip = str(data.get("ip") or "")
            _real_ip_cache = ip or None
    except (OSError, json.JSONDecodeError):
        pass


def _same_country(a: str, b: str) -> bool:
    """Compare two countries by code or name, accent- and case-insensitively."""
    return bool(a) and bool(b) and fold(resolve_country(a)) == fold(resolve_country(b))


# ---------------------------------------------------------------------------
# Resilient probing: the same echo services the container image uses, in parallel
# ---------------------------------------------------------------------------


def _json_ip(payload: str, key_map: dict[str, str]) -> JsonDoc | None:
    """Parse echo-service JSON into a common info dict; None when it has no IP."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("ip"):
        return None
    info: JsonDoc = {"ip": str(data["ip"])}
    for source, target in key_map.items():
        value = str(data.get(source) or "")
        if value:
            info[target] = value
    return info


def _extract_ipinfo(payload: str) -> JsonDoc | None:
    """ipinfo.io full JSON: ip, country, city, region, org."""
    keys = {"country": "country", "city": "city", "region": "region", "org": "org"}
    return _json_ip(payload, keys)


def _extract_trace(payload: str) -> JsonDoc | None:
    """Cloudflare trace text (one.one.one.one/cdn-cgi/trace): an `ip=` field."""
    for line in payload.splitlines():
        key, sep, value = line.partition("=")
        if sep and key == "ip" and value.strip():
            return {"ip": value.strip()}
    return None


def _extract_ifconfigco(payload: str) -> JsonDoc | None:
    """ifconfig.co echoip JSON: ip, country, city, region_name, asn_org."""
    return _json_ip(
        payload, {"country": "country", "city": "city", "region_name": "region", "asn_org": "org"}
    )


def _extract_ip2location(payload: str) -> JsonDoc | None:
    """api.ip2location.io JSON: ip, country_name, city_name, region_name, as."""
    return _json_ip(
        payload,
        {"country_name": "country", "city_name": "city", "region_name": "region", "as": "org"},
    )


_PROVIDERS: list[tuple[str, str, Callable[[str], JsonDoc | None]]] = [
    ("ipinfo", "https://ipinfo.io/", _extract_ipinfo),
    ("cloudflare", "https://one.one.one.one/cdn-cgi/trace", _extract_trace),
    ("ifconfigco", "https://ifconfig.co/json", _extract_ifconfigco),
    ("ip2location", "https://api.ip2location.io/", _extract_ip2location),
]


@dataclass(frozen=True)
class Probe:
    """One accepted probe observation and the providers that supplied it."""

    info: JsonDoc
    sources: tuple[str, ...]


def probe_provider(container: str, url: str) -> str:
    """One echo fetch from inside the container; '' on failure."""
    result = run(
        "docker",
        "exec",
        container,
        "timeout",
        str(PROBE_TIMEOUT),
        "wget",
        "-q",
        "-T",
        str(PROBE_TIMEOUT),
        "-O-",
        url,
        capture=True,
        check=False,
        timeout=PROBE_EXEC_TIMEOUT_S,
    )
    return result.stdout if result.returncode == 0 else ""


def _vote(results: list[tuple[str, JsonDoc]]) -> Probe:
    """Plurality over distinct IPs; ties broken by provider priority order."""
    order = {name: i for i, (name, _, _) in enumerate(_PROVIDERS)}
    by_ip: dict[str, list[tuple[str, JsonDoc]]] = {}
    for name, info in results:
        by_ip.setdefault(str(info["ip"]), []).append((name, info))

    best_entries: list[tuple[str, JsonDoc]] = []
    best_priority = len(_PROVIDERS)
    for _ip, entries in by_ip.items():
        priority = min(order[name] for name, _ in entries)
        if len(entries) > len(best_entries) or (
            len(entries) == len(best_entries) and best_entries and priority < best_priority
        ):
            best_entries, best_priority = entries, priority

    ordered = sorted(best_entries, key=lambda pair: order[pair[0]])
    sources = tuple(name for name, _ in ordered)
    merged: JsonDoc = {}
    for _name, entry in ordered:
        for key, value in entry.items():
            if key == "country":
                code = to_code(str(value or ""))
                if code:
                    merged.setdefault("country", COUNTRY_NAMES[code])
                continue
            merged.setdefault(key, value)
    return Probe(info=merged, sources=sources)


def probe(container: str | None = None) -> Probe | None:
    """One public-IP probe from inside a container. None when every provider failed.

    Providers run in parallel, so a single rate-limited service is absorbed
    rather than stalling the caller's retry loop; the most-agreed answer wins.
    """
    container = container or current_instance().container
    results: list[tuple[str, JsonDoc]] = []
    with ThreadPoolExecutor(max_workers=len(_PROVIDERS)) as pool:
        futures = {
            pool.submit(probe_provider, container, url): (name, extract)
            for name, url, extract in _PROVIDERS
        }
        for future in as_completed(futures):
            name, extract = futures[future]
            info = extract(future.result())
            if info:
                results.append((name, info))
    if not results:
        return None
    return _vote(results)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------


def fetch_ip_info(
    retries: int = IP_FETCH_RETRIES,
    delay: float = IP_FETCH_DELAY,
    expected_country: str | None = None,
    exclude_ips: Iterable[str] | None = None,
    container: str | None = None,
) -> IpOutcome:
    """Poll until the container's exit IP is outside exclude_ips (bare IP included).

    Stops at the first acceptable observation — country never gates success,
    `expected_country` only sets IpResult.matched. Progress notices distinguish
    leaks (still on the bare connection) from stale routes (previous exit).
    """
    exclude = set(exclude_ips or ())
    bare = real_ip()
    if bare:
        exclude.add(bare)

    last_info: JsonDoc | None = None
    for attempt in range(retries):
        result = probe(container=container)
        if result is None:
            if 0 < attempt < retries - 1:
                click.echo(f"Waiting for public IP... ({attempt + 1}/{retries})")
        else:
            info = result.info
            last_info = info
            ip = str(info.get("ip") or "")
            if not ip or ip in exclude:
                if not ip:
                    if 0 < attempt < retries - 1:
                        click.echo(f"Waiting for public IP... ({attempt + 1}/{retries})")
                elif bare and ip == bare:
                    click.echo(f"Leak: traffic not going through VPN ({attempt + 1}/{retries})")
                else:
                    country = resolve_country(str(info.get("country", "")))
                    click.echo(f"Still routed via {country} ({attempt + 1}/{retries})")
            else:
                matched = not expected_country or _same_country(
                    str(info.get("country", "")), expected_country
                )
                return IpOutcome(IpResult(info=info, matched=matched, sources=result.sources), info)
        if attempt < retries - 1:
            time.sleep(delay)
    return IpOutcome(last_info=last_info)


def _fmt_row(
    label: str,
    info: JsonDoc,
    color: str | None = None,
    marker: str = " ",
) -> str:
    """Format one row of IP info: IP, location, org."""
    ip = str(info.get("ip", "?"))
    country = resolve_country(str(info.get("country", "?")))
    location = f"{info.get('city', '?')}, {country}"
    org = str(info.get("org", "?"))
    text = f"  {marker} {ip:<20} {location:<25} {org}"
    if color:
        return click.style(text, fg=color)
    return text


def _fmt_table(
    exit_info: JsonDoc,
    exit_color: str | None,
    bare_info: JsonDoc | None,
) -> None:
    """Print the IP comparison table."""
    header = click.style(f"  {' ':3} {'IP':<20} {'Location':<25} {'Org'}", bold=True)
    click.echo(header)
    click.echo(_fmt_row("VPN", exit_info, exit_color, marker="▸"))
    if bare_info:
        click.echo(_fmt_row("Bare", bare_info, "bright_black"))


def print_ip_status(
    expected_country: str | None = None,
    exclude_ips: Iterable[str] | None = None,
) -> bool:
    """Fetch and display VPN and host IP info side by side with a tri-state verdict.

    Returns True only when traffic verifiably exits through the VPN. Fail-closed
    (C2): an empty observation is never success, and an unknown host bare IP
    degrades only to a loudly-warned country match — never a silent green.
    """
    bare = real_ip()
    outcome = fetch_ip_info(
        expected_country=expected_country,
        exclude_ips=exclude_ips,
    )
    host = real_ip_info()

    if outcome.result is None:
        last = outcome.last_info or {}
        last_ip = str(last.get("ip") or "")
        if bare and last_ip == bare:
            _fmt_table(last, "red", host)
            click.echo(click.style("  LEAK: VPN exit matches your bare connection", fg="red"))
        else:
            click.echo("Could not fetch public IP.")
        return False

    info = outcome.result.info
    exit_ip = str(info.get("ip") or "")
    sources = outcome.result.sources
    if "ipinfo" not in sources:
        backup = ", ".join(sources) or "unknown"
        click.echo(
            click.style(
                f"  Public IP confirmed via {backup} — ipinfo.io was rate-limited or unreachable",
                fg="yellow",
            )
        )

    if not exit_ip:
        click.echo("Could not fetch public IP.")
        return False

    state, _ = classify_verdict(
        bare, exit_ip, matched=outcome.result.matched, expected_country=expected_country
    )

    if state == "leak":
        _fmt_table(info, "red", host)
        click.echo(click.style("  LEAK: VPN exit matches your bare connection", fg="red"))
        return False
    if state == "unknown":
        _fmt_table(info, "yellow", host)
        if bare is None:
            click.echo(
                click.style(
                    "  Cannot verify: could not determine the host's bare IP (fail-closed)",
                    fg="yellow",
                )
            )
        else:
            click.echo(click.style("  Could not verify the tunnel exit.", fg="yellow"))
        return False
    if expected_country and not outcome.result.matched:
        _fmt_table(info, "yellow", host)
        click.echo(
            click.style(
                f"  Note: exit country differs from the requested {expected_country}",
                fg="yellow",
            )
        )
    elif bare is None and expected_country:
        # Degraded-but-accepted: the country heuristic vouches for the exit
        # while the host IP stays unknown. Warn loudly (never a silent green).
        _fmt_table(info, "yellow", host)
        click.echo(
            click.style(
                "  Verified via country match only (the host's bare IP is unknown).",
                fg="yellow",
            )
        )
    else:
        _fmt_table(info, "green", host)
    return True


def current_exit_ip(retries: int = CURRENT_EXIT_IP_RETRIES) -> str | None:
    """Best-effort single-shot read of the container's current exit IP."""
    outcome = fetch_ip_info(retries=retries)
    info = (outcome.result.info if outcome.result else None) or outcome.last_info
    if not info:
        return None
    ip = info.get("ip")
    return str(ip) if ip else None
