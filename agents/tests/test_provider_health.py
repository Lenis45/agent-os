from unittest.mock import Mock, patch

import requests

import provider_health


def test_provider_probe_retries_transient_network_failure():
    response = Mock(status_code=200)
    with patch("requests.request", side_effect=[requests.Timeout("TLS timeout"), response]) as request:
        with patch("provider_health.time.sleep"):
            result = provider_health._get("https://example.test", {}, 1)

    assert result is response
    assert request.call_count == 2


def test_provider_probe_does_not_retry_permanent_http_error():
    response = Mock(status_code=401)
    with patch("requests.request", return_value=response) as request:
        result = provider_health._post("https://example.test", {}, {"ping": True}, 1)

    assert result is response
    request.assert_called_once()
