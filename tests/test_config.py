"""Tests for .env file parsing."""

from epoxy import config
from epoxy.config import DEFAULT_CACHE_TTL, cache_ttl, read_env_file


def test_read_env_file_missing(tmp_path):
    assert read_env_file(tmp_path / "nope.env") == {}


def test_read_env_file_basic(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\nB = 2\n")
    assert read_env_file(path) == {"A": "1", "B": "2"}


def test_read_env_file_skips_comments_and_blanks(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# comment\n\n  # indented comment\nKEY=value\n")
    assert read_env_file(path) == {"KEY": "value"}


def test_read_env_file_value_with_equals(tmp_path):
    path = tmp_path / ".env"
    path.write_text('JSON={"a":"b"}\n')
    assert read_env_file(path) == {"JSON": '{"a":"b"}'}


def test_read_env_file_ignores_lines_without_equals(tmp_path):
    path = tmp_path / ".env"
    path.write_text("NOEQUALS\nKEY=1\n")
    assert read_env_file(path) == {"KEY": "1"}


def test_read_env_file_strips_surrounding_quotes(tmp_path):
    path = tmp_path / ".env"
    path.write_text("DQ=\"quoted value\"\nSQ='single'\n")
    assert read_env_file(path) == {"DQ": "quoted value", "SQ": "single"}


def test_read_env_file_tolerates_export_prefix(tmp_path):
    path = tmp_path / ".env"
    path.write_text("export SURFSHARK_KEY=abc-cba\n")
    assert read_env_file(path) == {"SURFSHARK_KEY": "abc-cba"}


def test_default_cache_dir_is_epoxy(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.default_cache_dir() == tmp_path / ".cache" / "epoxy"


def test_cache_ttl_default_and_fallback(monkeypatch):
    monkeypatch.delenv("EPOXY_CACHE_TTL", raising=False)
    assert cache_ttl() == DEFAULT_CACHE_TTL
    monkeypatch.setenv("EPOXY_CACHE_TTL", "60")
    assert cache_ttl() == 60
    monkeypatch.setenv("EPOXY_CACHE_TTL", "garbage")
    assert cache_ttl() == DEFAULT_CACHE_TTL
