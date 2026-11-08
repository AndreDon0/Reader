"""Logging setup shared by executable entry points."""

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class RedactingFormatter(logging.Formatter):
    """Remove configured credentials and URL secrets, including in tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        for name in ("DATABASE_URL", "VPN_SUBSCRIPTION_URL"):
            secret = os.environ.get(name)
            if secret:
                message = message.replace(secret, "[redacted connection URL]")

        def redact_url(match: re.Match) -> str:
            try:
                parts = urlsplit(match.group())
                return urlunsplit((
                    parts.scheme, parts.netloc.rsplit("@", 1)[-1],
                    parts.path, "", "",
                ))
            except ValueError:
                return "[redacted URL]"

        return re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+", redact_url, message)


def configure_logging() -> None:
    """Log to stderr and a rotating file; repeated calls replace our handlers."""
    root = logging.getLogger()
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO

    for handler in root.handlers[:]:
        if getattr(handler, "_reader_handler", False):
            root.removeHandler(handler)
            handler.close()

    root.setLevel(level)
    formatter = RedactingFormatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    def add_handler(handler: logging.Handler) -> None:
        handler.setFormatter(formatter)
        handler._reader_handler = True
        root.addHandler(handler)

    add_handler(logging.StreamHandler())
    log_file = os.environ.get("LOG_FILE", "logs/reader.log")
    if log_file:
        try:
            path = Path(log_file)
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[1] / path
            path.parent.mkdir(parents=True, exist_ok=True)
            add_handler(RotatingFileHandler(
                path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
            ))
        except (OSError, ValueError):
            logging.getLogger(__name__).exception(
                "Cannot open log file; continuing with console logging",
            )

    if not isinstance(getattr(logging, level_name, None), int):
        logging.getLogger(__name__).warning("Invalid LOG_LEVEL; using INFO")
