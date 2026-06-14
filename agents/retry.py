"""
retry — ретраи и безопасные обёртки для ненадёжных внешних вызовов (v3.0 hardening).

@net_retry  — декоратор: 3 попытки с экспоненциальной задержкой (для HTTP/IMAP/SMTP/LLM).
safe(fn, ...) — выполнить и не упасть: вернуть default, залогировать ошибку.
"""
import time
import functools

try:
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    _HAS_TENACITY = True
except Exception:
    _HAS_TENACITY = False


def net_retry(attempts: int = 3, base: float = 1.0, max_wait: float = 10.0):
    """Декоратор ретрая для сетевых вызовов."""
    if _HAS_TENACITY:
        return retry(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=base, max=max_wait),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )

    # Фолбэк без tenacity
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **kw):
            last = None
            for i in range(attempts):
                try:
                    return fn(*a, **kw)
                except Exception as e:
                    last = e
                    if i < attempts - 1:
                        time.sleep(min(base * (2 ** i), max_wait))
            raise last
        return wrap
    return deco


def safe(fn, *args, default=None, label: str = "", logger=None, **kwargs):
    """Выполнить fn; при любой ошибке вернуть default и залогировать (не падать)."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        msg = f"[safe] {label or getattr(fn, '__name__', 'call')} упал: {e}"
        (logger.warning(msg) if logger else print(msg))
        return default
