
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)

        for name in ("DATABASE_URL", "VPN_SUBSCRIPTION_URL"):
            if secret := os.getenv(name):
                message = message.replace(secret, "[REDACTED]")

        def redact(match):
            try:
                url = urlsplit(match.group())
                return urlunsplit((
                    url.scheme, url.netloc.split("@")[-1],
                    url.path, "", ""
                ))
            except ValueError:
                return "[REDACTED]"

        return re.sub(r'[a-zA-Z][\w+.-]*://[^\s<>"\']+', redact, message)


def configure_logging():
    root = logging.getLogger()
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    root.setLevel(level if level in logging.getLevelNamesMapping() else "INFO")

    for handler in root.handlers[:]:
        if getattr(handler, "_reader", False):
            root.removeHandler(handler)
            handler.close()

    formatter = RedactingFormatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    def add(handler):
        handler.setFormatter(formatter)
        handler._reader = True
        root.addHandler(handler)

    add(logging.StreamHandler())

    if log_file := os.getenv("LOG_FILE", "logs/reader.log"):
        try:
            path = Path(log_file)
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[1] / path
            path.parent.mkdir(parents=True, exist_ok=True)
            add(RotatingFileHandler(
                path, maxBytes=5_000_000, backupCount=3,
                encoding="utf-8"
            ))
        except OSError:
            logging.exception("Cannot open log file")


def check_none(value, name: str, level=logging.WARNING):
    if value is None:
        if level==logging.WARNING:
            logging.warning(f"None is assigned to {name}")
        else:
            logging.error(f"Cannot assign None to {name}")
    return value
