import json
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from google.auth.exceptions import RefreshError

import calendar_auth


class FakeCredentials:
    def __init__(self, valid: bool, refresh_impl=None):
        self.valid = valid
        self.expired = not valid
        self.refresh_token = "refresh-token"
        self._refresh_impl = refresh_impl

    def refresh(self, _request):
        if self._refresh_impl:
            self._refresh_impl(self)
        self.valid = True
        self.expired = False

    def to_json(self):
        return json.dumps({"state": "fresh"})


def test_concurrent_refresh_is_serialized_and_reloaded(tmp_path: Path):
    token = tmp_path / "token.json"
    token.write_text('{"state":"expired"}', encoding="utf-8")
    refresh_calls = []

    def refresh_impl(_credentials):
        refresh_calls.append("refresh")
        time.sleep(0.08)

    def loader(path, _scopes):
        state = json.loads(Path(path).read_text(encoding="utf-8"))["state"]
        return FakeCredentials(state == "fresh", refresh_impl)

    def load_once():
        return calendar_auth.load_calendar_credentials(
            token,
            credentials_loader=loader,
            request_factory=lambda: object(),
            sleep=lambda _seconds: None,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        credentials = list(executor.map(lambda _index: load_once(), range(2)))

    assert all(item.valid for item in credentials)
    assert refresh_calls == ["refresh"]
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(token.with_suffix(".json.lock").stat().st_mode) == 0o600


def test_invalid_grant_is_not_retried(tmp_path: Path):
    token = tmp_path / "token.json"
    token.write_text("expired", encoding="utf-8")
    calls = []

    def fail(_credentials):
        calls.append("refresh")
        raise RefreshError("invalid_grant: Token has been expired or revoked")

    with pytest.raises(RefreshError, match="invalid_grant"):
        calendar_auth.load_calendar_credentials(
            token,
            credentials_loader=lambda *_args: FakeCredentials(False, fail),
            request_factory=lambda: object(),
            sleep=lambda _seconds: None,
        )

    assert calls == ["refresh"]


def test_transient_refresh_failure_retries_then_succeeds(tmp_path: Path):
    token = tmp_path / "token.json"
    token.write_text("expired", encoding="utf-8")
    calls = []

    def flaky(_credentials):
        calls.append("refresh")
        if len(calls) == 1:
            raise OSError("network timeout")

    result = calendar_auth.load_calendar_credentials(
        token,
        credentials_loader=lambda *_args: FakeCredentials(False, flaky),
        request_factory=lambda: object(),
        sleep=lambda _seconds: None,
    )

    assert result.valid is True
    assert calls == ["refresh", "refresh"]
    assert json.loads(token.read_text(encoding="utf-8"))["state"] == "fresh"
