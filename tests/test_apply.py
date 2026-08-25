"""Tests for the runtime configuration engine (selection swaps + verification)."""

import fcntl
import os
from typing import Any

import pytest

from vpn import apply, config, control


@pytest.fixture(autouse=True)
def _lock(monkeypatch, tmp_path):
    """Redirect the advisory lockfile into the test sandbox."""
    monkeypatch.setattr(config, "LOCK_FILE", tmp_path / "settings.lock")


def selection(provider="surfshark", protocol="wireguard",
              country=None, city=None) -> apply.Selection:
    return apply.Selection(provider, protocol, country, city)


# ---------------------------------------------------------------------------
# Selection parsing
# ---------------------------------------------------------------------------


def full_doc() -> dict[str, Any]:
    return {
        "type": "openvpn",
        "provider": {
            "name": "surfshark",
            "server_selection": {"countries": ["Germany"], "cities": ["Berlin"]},
        },
    }


def test_from_doc_reads_all_fields():
    sel = apply.Selection.from_doc(full_doc())
    assert sel == selection("surfshark", "openvpn", "Germany", "Berlin")


def test_from_doc_minimal_doc_is_blank_selection():
    assert apply.Selection.from_doc({}) == selection("", "")


def test_from_doc_empty_lists_are_none():
    doc = {"type": "wireguard", "provider": {"server_selection": {"countries": [], "cities": []}}}
    sel = apply.Selection.from_doc(doc)
    assert sel.country is None and sel.city is None


def test_key_folds_case():
    folded = apply.Selection("Surfshark", "WireGuard", "Japan", "Tokyo").key
    assert folded == ("surfshark", "wireguard", "japan", "tokyo")


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def test_swap_lock_excludes_concurrent_holders():
    with apply.swap_lock():
        fd = os.open(config.LOCK_FILE, os.O_CREAT | os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_swap_lock_released_after_context():
    with apply.swap_lock():
        pass
    fd = os.open(config.LOCK_FILE, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# apply_location / restore_settings
# ---------------------------------------------------------------------------


def test_apply_location_puts_mutated_document(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(apply, "get_settings", lambda: full_doc())

    def fake_put(doc: dict[str, Any]) -> str:
        captured["doc"] = doc
        return "restarted"

    monkeypatch.setattr(apply, "put_settings", fake_put)
    apply.apply_location(selection("protonvpn", "wireguard", "Japan"))

    doc = captured["doc"]
    assert doc["provider"]["name"] == "protonvpn"
    assert doc["type"] == "wireguard"
    assert doc["provider"]["server_selection"]["countries"] == ["Japan"]


def test_apply_location_translates_404_to_upgrade_hint(monkeypatch):
    monkeypatch.setattr(apply, "get_settings", lambda: full_doc())
    monkeypatch.setattr(
        apply, "put_settings",
        lambda doc: (_ for _ in ()).throw(control.ControlError(404, "404 page not found")),
    )
    with pytest.raises(control.ControlError) as excinfo:
        apply.apply_location(selection())
    assert excinfo.value.status == 404
    assert "--pull" in excinfo.value.message


def test_apply_location_get_404_also_hinted(monkeypatch):
    def boom() -> dict[str, Any]:
        raise control.ControlError(404, "404 page not found")

    monkeypatch.setattr(apply, "get_settings", boom)
    with pytest.raises(control.ControlError) as excinfo:
        apply.apply_location(selection())
    assert "--pull" in excinfo.value.message


def test_apply_location_passes_other_errors_through(monkeypatch):
    monkeypatch.setattr(apply, "get_settings", lambda: {})

    def bad_country(doc: dict[str, Any]) -> str:
        raise control.ControlError(400, "the country specified is not valid")

    monkeypatch.setattr(apply, "put_settings", bad_country)
    with pytest.raises(control.ControlError) as excinfo:
        apply.apply_location(selection(country="Atlantis"))
    assert excinfo.value.status == 400
    assert "not valid" in excinfo.value.message


def test_restore_settings_puts_document_under_lock(monkeypatch):
    base = full_doc()
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(apply, "put_settings", lambda doc: seen.append(doc))
    apply.restore_settings(base)
    assert seen and seen[0] is base


# ---------------------------------------------------------------------------
# verify (IP-delta proof over the leak-first fetcher)
# ---------------------------------------------------------------------------


def ok_fetch(info: dict[str, Any], matched: bool = True):
    def fake(**_kwargs: Any) -> Any:
        from vpn.ipinfo import IpOutcome, IpResult

        return IpOutcome(IpResult(info, matched))

    return fake


def test_verify_success_without_geo(monkeypatch):
    monkeypatch.setattr(
        apply, "fetch_ip_info", ok_fetch({"ip": "1.1.1.1", "country": "DE"}, matched=True)
    )
    verdict = apply.verify(selection(country="Germany"))
    assert verdict.ok is True
    assert verdict.ip == "1.1.1.1"
    assert verdict.geo is None  # matched -> no geo flag


def test_verify_flags_geo_mismatch(monkeypatch):
    monkeypatch.setattr(
        apply, "fetch_ip_info", ok_fetch({"ip": "1.1.1.1", "country": "DE"}, matched=False)
    )
    verdict = apply.verify(selection(country="Germany"))
    assert verdict.ok is True
    assert verdict.geo == "Germany"


def test_verify_passes_prev_ip_as_exclusion(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_fetch(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return ok_fetch({"ip": "2.2.2.2"}, True)(**kwargs)

    monkeypatch.setattr(apply, "fetch_ip_info", fake_fetch)
    apply.verify(selection(), prev_ip="9.9.9.9")
    assert captured["exclude_ips"] == {"9.9.9.9"}
    apply.verify(selection())
    assert captured["exclude_ips"] is None


def test_verify_failure_classification(monkeypatch):
    from vpn.ipinfo import IpOutcome

    bare = selection()
    monkeypatch.setattr(apply, "real_ip", lambda: "203.0.113.7")
    monkeypatch.setattr(
        apply, "fetch_ip_info", lambda **_k: IpOutcome(last_info={"ip": "203.0.113.7"})
    )
    assert apply.verify(bare).reason == "leak"

    monkeypatch.setattr(apply, "real_ip", lambda: None)
    monkeypatch.setattr(
        apply, "fetch_ip_info", lambda **_k: IpOutcome(last_info={"ip": "5.5.5.5"})
    )
    assert apply.verify(bare, prev_ip="5.5.5.5").reason == "no reconnect"

    monkeypatch.setattr(apply, "fetch_ip_info", lambda **_k: IpOutcome())
    assert apply.verify(bare).reason == "no public IP"
