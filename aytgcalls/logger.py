"""Structured logging for aytgcalls.

All loggers are namespaced ``aytgcalls.*``. Debug mode dumps signaling JSON with
secrets redacted.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any

__all__ = ["get_logger", "enable_debug", "redact", "dump_signaling"]

_ROOT = "aytgcalls"

#: JSON keys whose values are secret-ish and must never reach the logs verbatim.
_SECRET_KEYS = frozenset(
    {
        "pwd",
        "password",
        "ice_pwd",
        "ice-pwd",
        "fingerprint",
        "access_hash",
        "string_session",
        "session_string",
        "api_hash",
        "bot_token",
        "phone_number",
        "auth_key",
        "invite_hash",
        "token",
    }
)


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger under the ``aytgcalls`` namespace."""
    return logging.getLogger(_ROOT if not name else f"{_ROOT}.{name}")


def enable_debug(*, level: int = logging.DEBUG, stream: Any = None) -> logging.Logger:
    """Attach a formatted handler to the ``aytgcalls`` logger and set its level."""
    logger = logging.getLogger(_ROOT)
    logger.setLevel(level)
    if not any(getattr(h, "_aytgcalls", False) for h in logger.handlers):
        handler = logging.StreamHandler(stream)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        handler._aytgcalls = True
        logger.addHandler(handler)
    return logger


def _mask(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= 8:
            return "***"
        return f"{value[:4]}…{value[-2:]} ({len(value)} chars)"
    return "***"


def redact(data: Any) -> Any:
    """Deep-copy ``data`` replacing secret-ish values with masked placeholders."""
    if isinstance(data, dict):
        out: dict[Any, Any] = {}
        for key, value in data.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEYS:
                out[key] = _mask(value)
            else:
                out[key] = redact(value)
        return out
    if isinstance(data, (list, tuple)):
        return [redact(item) for item in data]
    return copy.copy(data)


def dump_signaling(logger: logging.Logger, label: str, payload: Any) -> None:
    """Log a signaling payload at DEBUG level with secrets redacted.

    Skips all work (including the deep copy) when DEBUG is not enabled.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            logger.debug("%s: %s", label, payload)
            return
    logger.debug("%s: %s", label, json.dumps(redact(payload), indent=2, sort_keys=True))


if os.environ.get("AYTGCALLS_DEBUG", "").lower() in {"1", "true", "yes", "on"}:
    enable_debug()
