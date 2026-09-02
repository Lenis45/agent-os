"""Shared Telegram runtime policies for long-running Amori bots."""

import httpx

from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.request import HTTPXRequest


def telegram_http_request(*, polling: bool = False) -> HTTPXRequest:
    """Use IPv4 so a VPN's broken IPv6 route cannot stall Telegram."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=2)
    return HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=15,
        read_timeout=45 if polling else 30,
        write_timeout=30,
        pool_timeout=10,
        media_write_timeout=60,
        httpx_kwargs={"transport": transport},
    )


def post_json_ipv4(url: str, payload: dict, *, timeout: float = 20) -> dict:
    """Send a synchronous Bot API request over a deterministic IPv4 path."""
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=2)
    with httpx.Client(transport=transport, timeout=timeout) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
    return data if isinstance(data, dict) else {}


def install_error_handler(application, logger, component):
    async def handle_error(_update, context):
        error = context.error
        if isinstance(error, (NetworkError, TimedOut, RetryAfter)):
            logger.warning("Telegram transport recovered by polling loop: %s", error)
            return
        logger.error(
            "Unhandled Telegram error in %s",
            component,
            exc_info=(type(error), error, error.__traceback__),
        )

    application.add_error_handler(handle_error)
