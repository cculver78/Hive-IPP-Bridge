"""Redacted Windows application logging."""

from __future__ import annotations

import logging as std_logging
from logging.handlers import RotatingFileHandler

from .credentials import CredentialError, _ensure_windows_acl
from .paths import log_dir


class SecretRedactionFilter(std_logging.Filter):
    """Prevent obvious credential-bearing fields from reaching log handlers."""

    _sensitive = ("jwt", "token", "password", "authorization", "bearer", "setup_link")

    def filter(self, record: std_logging.LogRecord) -> bool:
        message = record.getMessage().lower()
        return not any(marker in message for marker in self._sensitive)


def get_logger(name: str = "hive_ipp_bridge", filename: str = "bridge.log") -> std_logging.Logger:
    logger = std_logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(std_logging.INFO)
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        _ensure_windows_acl(directory)
        log_path = directory / filename
        handler: std_logging.Handler = RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        _ensure_windows_acl(log_path)
    except (CredentialError, OSError):
        handler = std_logging.NullHandler()
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(std_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger
