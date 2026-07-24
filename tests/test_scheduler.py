"""
tests/test_scheduler.py
────────────────────────
Offline tests for follow-up timing. No network. Proves: FU1 at day 4, FU2 at
day 10, FU2 waits for FU1, and any reply cancels all follow-ups.

Run:
    python -m unittest tests.test_scheduler -v
"""

from __future__ import annotations

import datetime as dt
import unittest

from core.scheduler import FollowupState, due_followups


def _sent_days_ago(n: int) -> str:
    return (dt.datetime.now() - dt.timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")


class TestFollowups(unittest.TestCase):
    def test_nothing_due_before_day_4(self):
        st = FollowupState(_sent_days_ago(2), False, False, False)
        self.assertEqual(due_followups(st), [])

    def test_fu1_due_at_day_4(self):
        st = FollowupState(_sent_days_ago(4), False, False, False)
        self.assertEqual(due_followups(st), ["followup_1"])

    def test_fu2_waits_for_fu1(self):
        # Day 10 reached but FU1 never sent -> send FU1 first (and FU2 too, since
        # both are due), never FU2 alone.
        st = FollowupState(_sent_days_ago(10), False, False, False)
        self.assertEqual(due_followups(st), ["followup_1", "followup_2"])

    def test_fu2_after_fu1_sent(self):
        st = FollowupState(_sent_days_ago(10), True, False, False)
        self.assertEqual(due_followups(st), ["followup_2"])

    def test_reply_cancels_all(self):
        st = FollowupState(_sent_days_ago(30), False, False, True)
        self.assertEqual(due_followups(st), [])

    def test_all_sent_nothing_left(self):
        st = FollowupState(_sent_days_ago(30), True, True, False)
        self.assertEqual(due_followups(st), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
