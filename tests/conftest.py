"""Shared test fixtures: keep all per-instance / cache state out of ~/.cache."""

import pytest

from epoxy import config, instance
from tests.harness import TEST_INSTANCE


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Redirect registry, locks, and the server cache into the test sandbox."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "CACHE_FILE", tmp_path / "cache" / "servers.json")
    monkeypatch.setattr(config, "LOCKS_DIR", tmp_path / "cache" / "locks")
    monkeypatch.setattr(config, "INSTANCES_DIR", tmp_path / "cache" / "instances")


@pytest.fixture(autouse=True)
def default_instance(monkeypatch):
    """Name the instance that internal calls fall back to.

    Most tests drive the CLI, where ``--instance`` (injected by ``run_cli``)
    names the target. The rest call functions that resolve their instance from
    ``current_instance()`` — ``servers``, ``speedtest``, ``providers``,
    ``control`` — and those have no command line to carry a name on.

    This supplies only the name. It is a thunk rather than a pre-built
    ``Instance`` so ``build_env`` still reads ``os.environ`` when the test asks,
    not when the fixture was set up — several tests set provider credentials and
    then expect the instance to see them. Name resolution is deliberately left
    alone, so a CLI call that omits ``--instance`` still gets the usage error.

    Warning: this fallback also masks missing-context bugs in production paths
    (a call that reaches ``current_instance()`` outside ``instance_context``
    succeeds here but raises a usage error for real). Tests pinning such a path
    must disable the fallback explicitly.
    """
    monkeypatch.setattr(
        instance, "default_instance", lambda: instance.resolve_instance(TEST_INSTANCE)
    )
