from email.message import Message
import importlib.util
from pathlib import Path
import sys


SERVER_PATH = Path(__file__).parents[2] / "dashboard" / "server.py"
sys.path.insert(0, str(SERVER_PATH.parent))
SPEC = importlib.util.spec_from_file_location("dashboard_server_auth", SERVER_PATH)
dashboard_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard_server)


def handler(address="100.66.130.21", authorization="", path="/api/state"):
    value = dashboard_server.H.__new__(dashboard_server.H)
    value.client_address = (address, 45000)
    value.path = path
    value.headers = Message()
    if authorization:
        value.headers["Authorization"] = authorization
    return value


def test_loopback_requests_are_trusted_without_token(monkeypatch):
    monkeypatch.setattr(dashboard_server, "DASH_TOKEN", "configured")

    assert handler(address="127.0.0.1")._authed() is True
    assert handler(address="::1")._authed() is True


def test_remote_requests_require_matching_bearer_token(monkeypatch):
    monkeypatch.setattr(dashboard_server, "DASH_TOKEN", "configured")

    assert handler()._authed() is False
    assert handler(authorization="Bearer wrong")._authed() is False
    assert handler(authorization="bearer configured")._authed() is True


def test_query_string_cannot_carry_remote_access_token(monkeypatch):
    monkeypatch.setattr(dashboard_server, "DASH_TOKEN", "configured")

    assert handler(path="/api/state?t=configured")._authed() is False


def test_remote_access_fails_closed_when_token_is_not_configured(monkeypatch):
    monkeypatch.setattr(dashboard_server, "DASH_TOKEN", "")

    assert handler()._authed() is False
