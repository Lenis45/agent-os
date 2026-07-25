import json

import hypothesis_hub


def test_fetch_summary_sends_optional_token_and_returns_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"totals": {"all": 1}, "hypotheses": []}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["token"] = request.headers.get("X-amori-token")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("HYPOTHESIS_HUB_API_URL", "http://hub.local:3001/")
    monkeypatch.setenv("HYPOTHESIS_HUB_TOKEN", "test-token")
    monkeypatch.setattr(hypothesis_hub.urllib.request, "urlopen", fake_urlopen)

    assert hypothesis_hub.fetch_summary()["totals"]["all"] == 1
    assert captured == {"url": "http://hub.local:3001/api/amori/summary", "token": "test-token", "timeout": 8}
