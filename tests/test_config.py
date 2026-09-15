"""Tests for .env file parsing and configuration resolution."""

from vpn.config import read_env_file, resolve_compose_file


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
    path.write_text('DQ="quoted value"\nSQ=\'single\'\n')
    assert read_env_file(path) == {"DQ": "quoted value", "SQ": "single"}


def test_read_env_file_tolerates_export_prefix(tmp_path):
    path = tmp_path / ".env"
    path.write_text("export SURFSHARK_KEY=abc-cba\n")
    assert read_env_file(path) == {"SURFSHARK_KEY": "abc-cba"}


def test_resolve_prefers_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom.yml"
    monkeypatch.setenv("GLUETUN_COMPOSE_FILE", str(override))
    assert resolve_compose_file() == str(override)


def test_resolve_prefers_local_vpn_yml(monkeypatch, tmp_path):
    monkeypatch.delenv("GLUETUN_COMPOSE_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vpn.yml").write_text("services: {}\n")
    assert resolve_compose_file() == str(tmp_path / "vpn.yml")


def test_resolve_falls_back_to_packaged_copy(monkeypatch, tmp_path):
    monkeypatch.delenv("GLUETUN_COMPOSE_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    resolved = resolve_compose_file()
    assert resolved.endswith("vpn.yml") and resolved != str(tmp_path / "vpn.yml")
