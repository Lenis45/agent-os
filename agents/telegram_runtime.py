"""Shared Telegram runtime policies for long-running Amori bots."""

from telegram.error import NetworkError, RetryAfter, TimedOut


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
