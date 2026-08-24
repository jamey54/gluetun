"""Tests for the control server client and settings document building."""

import io
import json
import urllib.error
from typing import Any

import pytest

from vpn import control


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body.encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def fake_urllib(status_body: tuple[int, str], calls: list[Any] | None = None):
    """Return a urlopen replacement recording requests and answering once."""
    log = calls if calls is not None else []

    def opener(request: Any, timeout: float = 10) -> FakeResponse:
        log.append(
            (
                request.get_method(),
                request.full_url,
                request.data,
                dict(request.header_items()),
            )
        )
        return FakeResponse(*status_body)

    return opener


def url_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x", code, "err", {}, io.BytesIO(body.encode())  # type: ignore[arg-type]
    )


def sample_doc() -> dict[str, Any]:
    return {
        "type": "wireguard",
        "provider": {
            "name": "surfshark",
            "server_selection": {
                "vpn": "wireguard",
                "countries": ["Netherlands"],
                "cities": ["Amsterdam"],
                "hostnames": ["nl-1.prod.surfshark.com"],
            },
        },
        "wireguard": {"private_key": "old-key", "addresses": ["10.14.0.2/16"]},
        "openvpn": {"user": "", "password": ""},
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HTTP_CONTROL_SERVER_API_KEY", "secret-key")
    monkeypatch.delenv("HTTP_CONTROL_SERVER_ADDRESS", raising=False)


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


def test_base_url_default():
    assert control.base_url() == "http://127.0.0.1:8000"


def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("HTTP_CONTROL_SERVER_ADDRESS", "http://localhost:8005/")
    assert control.base_url() == "http://localhost:8005"


def test_get_settings_sends_auth_header_and_parses(monkeypatch):
    calls: list[Any] = []
    doc = sample_doc()
    monkeypatch.setattr(
        control, "urlopen", fake_urllib((200, json.dumps(doc)), calls)
    )
    assert control.get_settings() == doc
    method, url, _, headers = calls[0]
    assert (method, url) == ("GET", "http://127.0.0.1:8000/v1/vpn/settings")
    assert any(k.lower() == "x-api-key" and v == "secret-key" for k, v in headers.items())


def test_put_settings_posts_json_and_returns_outcome(monkeypatch):
    calls: list[Any] = []
    monkeypatch.setattr(control, "urlopen", fake_urllib((200, "restarted"), calls))
    doc = {"type": "wireguard"}
    assert control.put_settings(doc) == "restarted"
    method, url, data, headers = calls[0]
    assert (method, url) == ("PUT", "http://127.0.0.1:8000/v1/vpn/settings")
    assert json.loads(data) == doc
    items = {k.lower(): v for k, v in headers.items()}
    assert items["content-type"] == "application/json"
    assert items["x-api-key"] == "secret-key"


def test_http_error_raises_control_error(monkeypatch):
    def boom(request: Any, timeout: float = 10) -> None:
        raise url_error(400, "the country specified is not valid")

    monkeypatch.setattr(control, "urlopen", boom)
    with pytest.raises(control.ControlError) as excinfo:
        control.get_settings()
    assert excinfo.value.status == 400
    assert "not valid" in excinfo.value.message


def test_connection_error_wrapped(monkeypatch):
    def boom(request: Any, timeout: float = 10) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(control, "urlopen", boom)
    with pytest.raises(control.ControlError) as excinfo:
        control.get_settings()
    assert excinfo.value.status is None
    assert "connection refused" in str(excinfo.value)


def test_get_settings_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(control, "urlopen", fake_urllib((200, "<html>nope</html>")))
    with pytest.raises(control.ControlError):
        control.get_settings()


def test_route_supported_true(monkeypatch):
    monkeypatch.setattr(
        control, "urlopen", fake_urllib((200, json.dumps(sample_doc())))
    )
    assert control.settings_route_supported() is True


def test_route_supported_false_on_404(monkeypatch):
    def boom(request: Any, timeout: float = 10) -> None:
        raise url_error(404, "404 page not found")

    monkeypatch.setattr(control, "urlopen", boom)
    assert control.settings_route_supported() is False


def test_route_supported_propagates_other_errors(monkeypatch):
    def boom(request: Any, timeout: float = 10) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(control, "urlopen", boom)
    with pytest.raises(control.ControlError):
        control.settings_route_supported()


# ---------------------------------------------------------------------------
# with_location document building
# ---------------------------------------------------------------------------


def test_with_location_sets_type_provider_and_selection():
    out = control.with_location(sample_doc(), "protonvpn", "wireguard", "Japan")
    assert out["type"] == "wireguard"
    assert out["provider"]["name"] == "protonvpn"
    sel = out["provider"]["server_selection"]
    assert sel["countries"] == ["Japan"]
    assert sel["cities"] == []


def test_with_location_clears_stale_city_and_filters():
    out = control.with_location(sample_doc(), "surfshark", "wireguard", "Germany")
    sel = out["provider"]["server_selection"]
    assert sel["cities"] == []  # stale Amsterdam dropped
    for field in ("regions", "categories", "isps", "hostnames", "names", "numbers"):
        assert sel[field] == [], field


def test_with_location_city_candidate():
    out = control.with_location(sample_doc(), "surfshark", "openvpn", "United States", "Boston")
    sel = out["provider"]["server_selection"]
    assert sel["countries"] == ["United States"]
    assert sel["cities"] == ["Boston"]
    assert out["type"] == "openvpn"


def test_with_location_does_not_mutate_input():
    doc = sample_doc()
    control.with_location(doc, "protonvpn", "openvpn", "Iceland")
    assert doc["provider"]["name"] == "surfshark"
    assert doc["provider"]["server_selection"]["cities"] == ["Amsterdam"]


def test_with_location_handles_minimal_doc():
    out = control.with_location({}, "surfshark", "wireguard", "France", "Paris")
    sel = out["provider"]["server_selection"]
    assert sel["countries"] == ["France"]
    assert sel["cities"] == ["Paris"]
    assert sel["vpn"] == "wireguard"


def test_with_location_injects_wireguard_credentials(monkeypatch):
    monkeypatch.setenv("PROTONVPN_WIREGUARD_PRIVATE_KEY", "proton-key")
    monkeypatch.setenv("PROTONVPN_WIREGUARD_ADDRESSES", "10.2.0.2/32")
    out = control.with_location(sample_doc(), "protonvpn", "wireguard", "Japan")
    wg = out["wireguard"]
    assert wg["private_key"] == "proton-key"
    assert wg["addresses"] == ["10.2.0.2/32"]


def test_with_location_splits_comma_separated_addresses(monkeypatch):
    monkeypatch.setenv("SURFSHARK_WIREGUARD_PRIVATE_KEY", "sk-key")
    monkeypatch.setenv("SURFSHARK_WIREGUARD_ADDRESSES", "10.14.0.2/16, 172.16.0.2/16")
    out = control.with_location(sample_doc(), "surfshark", "wireguard", "Germany")
    assert out["wireguard"]["addresses"] == ["10.14.0.2/16", "172.16.0.2/16"]


def test_with_location_injects_openvpn_credentials(monkeypatch):
    monkeypatch.setenv("SURFSHARK_OPENVPN_USER", "u123")
    monkeypatch.setenv("SURFSHARK_OPENVPN_PASSWORD", "p456")
    out = control.with_location(sample_doc(), "surfshark", "openvpn", "Poland")
    ovpn = out["openvpn"]
    assert ovpn["user"] == "u123"
    assert ovpn["password"] == "p456"
