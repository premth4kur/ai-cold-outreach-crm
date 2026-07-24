"""
utils/logger.py
───────────────
Central logging setup. Creates the logs/ directory and wires up separate,
rotating destinations so problems are easy to find after the fact:

  logs/daily-YYYY-MM-DD.log   — everything from today's run (INFO+)
  logs/smtp.log               — SMTP sends and responses only
  logs/activity.log           — high-level per-lead activity (mirror of the sheet)
  logs/errors.log             — WARNING+ across the whole app
  (weekly view is just the set of daily files for the week)

Usage:
    from utils.logger import get_logger, setup_logging
    setup_logging()              # once, at program start
    log = get_logger(__name__)
    log.info("…")

    from utils.logger import smtp_log, activity_log
    smtp_log("Sent to x@y.com: 250 OK")
"""

from __future__ import annotations

import datetime as dt
import logging
from logging.handlers import RotatingFileHandler

from config.settings import settings

_CONFIGURED = False
_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def _handler(path, level, fmt=_FMT) -> RotatingFileHandler:
    h = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(logging.Formatter(fmt))
    return h


def setup_logging(verbose: bool = True) -> None:
    """Configure root + dedicated loggers. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    logs_dir = settings.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Console
    console = logging.StreamHandler()
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)-7s | %(message)s"))
    root.addHandler(console)

    # Daily (all) + errors
    root.addHandler(_handler(logs_dir / f"daily-{today}.log", logging.INFO))
    root.addHandler(_handler(logs_dir / "errors.log", logging.WARNING))

    # Dedicated SMTP + activity loggers (do not propagate to root's daily file
    # twice — they get their own files and still bubble errors up).
    for name, filename in (("smtp", "smtp.log"), ("activity", "activity.log")):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.addHandler(_handler(logs_dir / filename, logging.INFO))
        lg.propagate = False

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)


def smtp_log(message: str, level: int = logging.INFO) -> None:
    logging.getLogger("smtp").log(level, message)


def activity_log(message: str, level: int = logging.INFO) -> None:
    logging.getLogger("activity").log(level, message)
