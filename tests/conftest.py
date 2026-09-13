"""Shared test fixtures: keep all per-instance / cache state out of ~/.cache."""

import pytest

from vpn import config


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Redirect registry, locks, and the server cache into the test sandbox."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "CACHE_FILE", tmp_path / "cache" / "servers.json")
    monkeypatch.setattr(config, "LOCKS_DIR", tmp_path / "cache" / "locks")
    monkeypatch.setattr(config, "INSTANCES_DIR", tmp_path / "cache" / "instances")
