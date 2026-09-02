import asyncio
from types import SimpleNamespace

from telegram.error import NetworkError

import telegram_runtime
from telegram_runtime import install_error_handler


class Application:
    def add_error_handler(self, callback):
        self.callback = callback


class Logger:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, message, *args):
        self.warnings.append(message % args)

    def error(self, message, *args, **kwargs):
        self.errors.append((message % args, kwargs))


def test_network_errors_are_logged_without_traceback():
    application = Application()
    logger = Logger()
    install_error_handler(application, logger, "bot")

    asyncio.run(application.callback(None, SimpleNamespace(error=NetworkError("TLS EOF"))))

    assert logger.warnings == ["Telegram transport recovered by polling loop: TLS EOF"]
    assert not logger.errors


def test_programming_errors_keep_traceback_context():
    application = Application()
    logger = Logger()
    install_error_handler(application, logger, "bot")
    error = RuntimeError("broken handler")

    asyncio.run(application.callback(None, SimpleNamespace(error=error)))

    assert not logger.warnings
    assert logger.errors[0][0] == "Unhandled Telegram error in bot"
    assert logger.errors[0][1]["exc_info"][1] is error


def test_telegram_request_forces_ipv4_transport(monkeypatch):
    sentinel = object()
    captured = {}

    def transport(**kwargs):
        captured["transport"] = kwargs
        return sentinel

    def request(**kwargs):
        captured["request"] = kwargs
        return "request"

    monkeypatch.setattr(telegram_runtime.httpx, "AsyncHTTPTransport", transport)
    monkeypatch.setattr(telegram_runtime, "HTTPXRequest", request)

    assert telegram_runtime.telegram_http_request(polling=True) == "request"
    assert captured["transport"]["local_address"] == "0.0.0.0"
    assert captured["request"]["httpx_kwargs"]["transport"] is sentinel
    assert captured["request"]["read_timeout"] == 45
