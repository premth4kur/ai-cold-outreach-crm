"""
utils/delays.py
───────────────
Random human-like pauses between sends (4–10 minutes by default, configurable
in .env). Randomised gaps make the sending pattern look natural and protect
deliverability.

DRY_RUN collapses the wait to near-zero so you can rehearse the whole pipeline
quickly without sitting through real delays.
"""

from __future__ import annotations

import random
import time

from config.settings import settings
from utils.logger import get_logger

log = get_logger(__name__)


def random_send_delay_seconds() -> int:
    """Pick a random delay, in seconds, within the configured minute window."""
    lo = settings.behaviour.delay_min_minutes
    hi = settings.behaviour.delay_max_minutes
    if hi < lo:
        lo, hi = hi, lo
    return random.randint(lo * 60, hi * 60)


def wait_between_sends() -> None:
    """Sleep for a random inter-send delay. No-op-ish under DRY_RUN."""
    if settings.behaviour.dry_run:
        log.info("DRY_RUN: skipping inter-send delay.")
        time.sleep(1)
        return
    secs = random_send_delay_seconds()
    log.info("Waiting %d min %02d sec before next send…", secs // 60, secs % 60)
    time.sleep(secs)
