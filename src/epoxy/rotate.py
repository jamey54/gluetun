"""Random location candidates for `epoxy rotate`.

Pure helpers keep the command thin and the randomness testable: callers pass
the cached server rows and an RNG, and get back unique ``(country, city)``
locations for one provider/protocol pair.
"""

import random
from collections.abc import Iterable, Sequence

from epoxy.config import DEFAULT_PROTOCOL
from epoxy.servers import ServerRow
from epoxy.textutil import fold

Location = tuple[str, str | None]


def folded_location(country: str | None, city: str | None) -> tuple[str, str]:
    """Comparison key for a location (accent- and case-insensitive)."""
    return fold(country or ""), fold(city or "")


def location_pool(
    rows: Iterable[ServerRow],
    provider: str,
    protocol: str,
    country: str | None = None,
    exclude: Iterable[tuple[str, str]] = (),
) -> list[Location]:
    """Unique locations for a provider/protocol pair, sorted for determinism.

    ``country`` narrows the pool by folded name; ``exclude`` drops locations by
    their :func:`folded_location` key (the current location, or picks already
    used by another instance).
    """
    excluded = set(exclude)
    seen: set[tuple[str, str]] = set()
    locations: list[Location] = []
    for row in rows:
        row_protocol = (row.get("vpn") or DEFAULT_PROTOCOL).lower()
        if row_protocol != protocol.lower():
            continue
        row_country = row.get("country", "")
        if not row_country:
            continue
        if country is not None and fold(row_country) != fold(country):
            continue
        row_city = row.get("city") or None
        key = folded_location(row_country, row_city)
        if key in excluded or key in seen:
            continue
        seen.add(key)
        locations.append((row_country, row_city))
    return sorted(locations, key=lambda loc: folded_location(*loc))


def shuffle_locations(locations: Sequence[Location], rng: random.Random) -> list[Location]:
    """A shuffled copy of the pool; the RNG is injected so tests stay stable."""
    shuffled = list(locations)
    rng.shuffle(shuffled)
    return shuffled
