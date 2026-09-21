"""Tests for ISO country code / name resolution."""

import pytest

from epoxy.countries import COUNTRY_NAMES, resolve_country, to_code


def test_to_code_from_alpha2():
    assert to_code("US") == "US"
    assert to_code("de") == "DE"


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("United States", "US"),
        ("Germany", "DE"),
        ("united states", "US"),
        ("türkiye", "TR"),
        ("turkey", "TR"),
        ("vietnam", "VN"),
        ("czech republic", "CZ"),
        ("russia", "RU"),
    ],
)
def test_to_code_from_name(name, code):
    assert to_code(name) == code


def test_to_code_unknown():
    assert to_code("XX") is None
    assert to_code("Atlantis") is None
    assert to_code("") is None


def test_to_code_two_letter_not_iso():
    # two letters that are not an assigned code must not pass through
    assert to_code("ZZ") is None


def test_resolve_country_known():
    assert resolve_country("DE") == "Germany"
    assert resolve_country("Netherlands") == "Netherlands"


def test_resolve_country_passthrough_unknown():
    assert resolve_country("Atlantis") == "Atlantis"
    assert resolve_country("?") == "?"


def test_all_codes_resolve_to_themselves():
    for code in COUNTRY_NAMES:
        assert to_code(code) == code
