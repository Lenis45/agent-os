"""
applog — структурное логирование для агентов (v3.0 hardening), вместо print().

get_logger("email_watchdog") → логгер с временем/уровнем в stdout
(launchd кладёт stdout в <agent>.log). Уровень из LOG_LEVEL (по умолчанию INFO).
"""
import os
import sys
import logging

_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_configured = set()


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if name not in _configured:
        logger.setLevel(getattr(logging, _LEVEL, logging.INFO))
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(h)
        logger.propagate = False
        _configured.add(name)
    return logger
