"""Tests for speed measurement helpers and CLI country comparison."""

from vpn.cli import _same_country
from vpn.speedtest import DEFAULT_SIZE_MB, mbps


def test_mbps():
    # 25,000,000 bytes in 10 s -> 20 Mbit/s
    assert mbps(25_000_000, 10) == 20
    assert mbps(1_000_000, 1) == 8


def test_default_size():
    assert DEFAULT_SIZE_MB == 25


def test_same_country_code_vs_name():
    assert _same_country("DE", "Germany")
    assert _same_country("United States", "US")


def test_same_country_accent_insensitive():
    assert _same_country("Curaçao", "Curacao")


def test_same_country_mismatch():
    assert not _same_country("DE", "FR")


def test_same_country_empty_strings():
    # empty vs empty must not count as a match
    assert not _same_country("", "")
