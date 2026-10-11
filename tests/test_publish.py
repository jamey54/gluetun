"""Tests for --publish spec parsing and canonicalization."""

import click
import pytest

from epoxy.publish import canonicalize, parse_publish_spec, spec_host_port


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("3000", ("127.0.0.1", 3000, 3000)),
        (" 3000 ", ("127.0.0.1", 3000, 3000)),
        ("8080:80", ("127.0.0.1", 8080, 80)),
        ("127.0.0.1:8080:80", ("127.0.0.1", 8080, 80)),
        ("0.0.0.0:3000:3000", ("0.0.0.0", 3000, 3000)),
        ("[::1]:8080:80", ("::1", 8080, 80)),
        ("1", ("127.0.0.1", 1, 1)),
        ("65535:65535", ("127.0.0.1", 65535, 65535)),
    ],
)
def test_parse_publish_spec_shapes(spec, expected):
    assert parse_publish_spec(spec) == expected


@pytest.mark.parametrize(
    "spec",
    ["", "  ", "abc", "3000:", ":80", ":", "0", "65536", "-1", "80:99999", "1.2.3.4", " : : "],
)
def test_parse_publish_spec_malformed_is_usage_error(spec):
    with pytest.raises(click.UsageError, match="Invalid --publish spec"):
        parse_publish_spec(spec)


def test_parse_publish_spec_names_bad_bind_address():
    with pytest.raises(click.UsageError, match="bad bind address"):
        parse_publish_spec(" :8080:80")


def test_canonicalize_binds_bare_ports_to_localhost():
    assert canonicalize("3000") == "127.0.0.1:3000:3000"
    assert canonicalize("8080:80") == "127.0.0.1:8080:80"
    assert canonicalize("0.0.0.0:3000:3000") == "0.0.0.0:3000:3000"


def test_spec_host_port_reads_stored_form():
    assert spec_host_port("127.0.0.1:3000:3000") == 3000
    assert spec_host_port("not-a-spec") is None
    assert spec_host_port("") is None
