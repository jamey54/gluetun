"""Resilient multi-provider probe: extractors, voting, parallel fetch.

The probe mirrors gluetun's approach — several echo services queried in
parallel, most-agreed result wins — so one rate-limited provider (ipinfo 429)
can no longer stall the verification retry loop.
"""

from subprocess import CompletedProcess

from vpn import ipinfo

_URLS = {
    "ipinfo": "https://ipinfo.io/",
    "cloudflare": "https://one.one.one.one/cdn-cgi/trace",
    "ifconfigco": "https://ifconfig.co/json",
    "ip2location": "https://api.ip2location.io/",
}


def stub_run(monkeypatch, results: dict[str, tuple[int, str]]) -> None:
    """Stub vpn.ipinfo.run to return per-provider (returncode, stdout) by URL."""

    def fake_run(*args, **kwargs):
        url = args[-1]
        code, payload = results.get(url, (1, ""))
        return CompletedProcess(
            ("docker", "exec", url), code, stdout=payload, stderr=""
        )

    monkeypatch.setattr(ipinfo, "run", fake_run)


# ---------------------------------------------------------------------------
# extractors
# ---------------------------------------------------------------------------


def test_extract_ipinfo_full():
    payload = (
        '{"ip": "1.2.3.4", "country": "DE", "city": "Berlin", "region": "Berlin", "org": "AS123"}'
    )
    assert ipinfo._extract_ipinfo(payload) == {
        "ip": "1.2.3.4",
        "country": "DE",
        "city": "Berlin",
        "region": "Berlin",
        "org": "AS123",
    }


def test_extract_ipinfo_rate_limit_body_is_none():
    assert ipinfo._extract_ipinfo('{"status": 429, "error": {"title": "Rate limit hit"}}') is None


def test_extract_ipinfo_bad_json_is_none():
    assert ipinfo._extract_ipinfo("<html>gateway error</html>") is None


def test_extract_cloudflare_trace():
    assert ipinfo._extract_trace("ip=5.6.7.8\nloc=DE\nwarp=off\n") == {"ip": "5.6.7.8"}
    assert ipinfo._extract_trace("loc=DE\nwarp=off\n") is None


def test_extract_ifconfigco_maps_fields():
    payload = (
        '{"ip":"1.2.3.4","country":"Germany","city":"Frankfurt",'
        '"region_name":"Hesse","asn_org":"AS1 Y"}'
    )
    assert ipinfo._extract_ifconfigco(payload) == {
        "ip": "1.2.3.4",
        "country": "Germany",
        "city": "Frankfurt",
        "region": "Hesse",
        "org": "AS1 Y",
    }


def test_extract_ip2location_maps_fields():
    payload = (
        '{"ip":"1.2.3.4","country_name":"Germany","city_name":"Frankfurt",'
        '"region_name":"Hesse","as":"AS1"}'
    )
    assert ipinfo._extract_ip2location(payload) == {
        "ip": "1.2.3.4",
        "country": "Germany",
        "city": "Frankfurt",
        "region": "Hesse",
        "org": "AS1",
    }


def test_extract_ip2location_error_body_is_none():
    assert ipinfo._extract_ip2location('{"error": {"message": "rate limited"}}') is None


# ---------------------------------------------------------------------------
# voting
# ---------------------------------------------------------------------------


def test_vote_plurality_wins_and_reports_all_sources():
    probe = ipinfo._vote(
        [
            ("ipinfo", {"ip": "1.1.1.1", "country": "US"}),
            ("cloudflare", {"ip": "1.1.1.1"}),
            ("ifconfigco", {"ip": "2.2.2.2"}),
        ]
    )
    assert probe.info["ip"] == "1.1.1.1"
    assert probe.sources == ("ipinfo", "cloudflare")  # priority order, not fetch order


def test_vote_tie_prefers_higher_priority_provider():
    probe = ipinfo._vote([("cloudflare", {"ip": "2.2.2.2"}), ("ipinfo", {"ip": "1.1.1.1"})])
    assert probe.info["ip"] == "1.1.1.1"


def test_vote_keeps_geodata_of_winning_group():
    probe = ipinfo._vote(
        [
            ("ipinfo", {"ip": "1.1.1.1", "country": "DE", "org": "ACME"}),
            ("cloudflare", {"ip": "1.1.1.1"}),
        ]
    )
    assert probe.info == {"ip": "1.1.1.1", "country": "Germany", "org": "ACME"}


def test_vote_single_provider_answer_is_accepted():
    probe = ipinfo._vote([("ifconfigco", {"ip": "7.7.7.7", "country": "FR"})])
    assert probe.info["ip"] == "7.7.7.7"
    assert probe.sources == ("ifconfigco",)


def test_vote_merges_geo_from_winning_group():
    """Cloudflare reports only the IP; geo/org come from the agreeing providers."""
    probe = ipinfo._vote(
        [
            ("cloudflare", {"ip": "1.1.1.1"}),
            (
                "ifconfigco",
                {"ip": "1.1.1.1", "country": "Germany", "city": "Frankfurt", "org": "AS1"},
            ),
            ("ip2location", {"ip": "1.1.1.1", "country": "Germany", "region": "Hesse"}),
        ]
    )
    assert probe.info == {
        "ip": "1.1.1.1",
        "country": "Germany",
        "city": "Frankfurt",
        "region": "Hesse",
        "org": "AS1",
    }
    assert probe.sources == ("cloudflare", "ifconfigco", "ip2location")


def test_vote_merge_keeps_highest_priority_field_values():
    probe = ipinfo._vote(
        [
            ("cloudflare", {"ip": "1.1.1.1"}),
            ("ip2location", {"ip": "1.1.1.1", "country": "Germany", "org": "AS-ALT"}),
            ("ipinfo", {"ip": "1.1.1.1", "country": "DE"}),
        ]
    )
    assert probe.info["country"] == "Germany"  # ipinfo's "DE" wins, canonicalized
    assert probe.info["org"] == "AS-ALT"  # filled from ip2location


def test_vote_canonicalizes_country_codes_and_names():
    name_first = ipinfo._vote([("ip2location", {"ip": "1.1.1.1", "country": "Germany"})])
    assert name_first.info["country"] == "Germany"

    code_first = ipinfo._vote([("ipinfo", {"ip": "1.1.1.1", "country": "FR"})])
    assert code_first.info["country"] == "France"


def test_vote_drops_non_iso_country():
    """A POP-style code (cloudflare loc=) never becomes a bogus country."""
    probe = ipinfo._vote([("cloudflare", {"ip": "1.1.1.1", "country": "WAW"})])
    assert "country" not in probe.info


def test_vote_drops_non_iso_country_but_keeps_other_geo():
    badge = ipinfo._vote(
        [
            ("cloudflare", {"ip": "1.1.1.1", "country": "WAW", "city": "Warsaw"}),
            ("ip2location", {"ip": "1.1.1.1", "country": "Poland"}),
        ]
    )
    assert badge.info["country"] == "Poland"
    assert badge.info["city"] == "Warsaw"


# ---------------------------------------------------------------------------
# parallel probe
# ---------------------------------------------------------------------------


def test_probe_survives_rate_limited_primary(monkeypatch):
    """ipinfo 429s; cloudflare still answers -> exit IP reported on first poll."""
    stub_run(
        monkeypatch,
        {
            _URLS["ipinfo"]: (4, '{"status": 429, "error": {}}'),
            _URLS["cloudflare"]: (0, "ip=5.6.7.8\nloc=DE\n"),
            _URLS["ifconfigco"]: (4, ""),
            _URLS["ip2location"]: (4, ""),
        },
    )
    result = ipinfo._probe("gluetun")
    assert result is not None
    assert result.info["ip"] == "5.6.7.8"
    assert result.sources == ("cloudflare",)


def test_probe_majority_across_providers(monkeypatch):
    stub_run(
        monkeypatch,
        {
            _URLS["ipinfo"]: (0, '{"ip": "1.1.1.1", "country": "DE"}'),
            _URLS["cloudflare"]: (0, "ip=1.1.1.1\n"),
            _URLS["ifconfigco"]: (0, '{"ip":"9.9.9.9"}'),
            _URLS["ip2location"]: (4, ""),
        },
    )
    result = ipinfo._probe("gluetun")
    assert result is not None
    assert result.info["ip"] == "1.1.1.1"
    assert result.sources == ("ipinfo", "cloudflare")


def test_probe_returns_none_when_every_provider_fails(monkeypatch):
    stub_run(monkeypatch, {})
    assert ipinfo._probe("gluetun") is None


def test_probe_uses_explicit_container(monkeypatch):
    seen: list[tuple[str, ...]] = []

    def fake_run(*args, **kwargs):
        seen.append(args)
        return CompletedProcess(("docker", "exec", "c"), 1, stdout="", stderr="")

    monkeypatch.setattr(ipinfo, "run", fake_run)
    assert ipinfo._probe("plan-a") is None
    assert all(args[:3] == ("docker", "exec", "plan-a") for args in seen)
    assert len(seen) == len(ipinfo._PROVIDERS)
