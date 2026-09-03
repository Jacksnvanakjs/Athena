"""首日回测档位规则。"""

import unittest
from datetime import datetime, timezone

from app.deal_monitor.first_day import (
    BAND_HIGH,
    BAND_LOW,
    BAND_MID,
    BAND_MID_HIGH,
    reaction_start_date,
    score_first_day_return,
)


class TestFirstDayScore(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(score_first_day_return(0.04).band, BAND_HIGH)
        self.assertEqual(score_first_day_return(0.04).score, 85)
        self.assertFalse(score_first_day_return(0.04).anomaly)

        self.assertEqual(score_first_day_return(0.015).band, BAND_MID_HIGH)
        self.assertEqual(score_first_day_return(0.015).score, 70)
        self.assertFalse(score_first_day_return(0.015).anomaly)

        mid = score_first_day_return(0.0)
        self.assertEqual(mid.band, BAND_MID)
        self.assertEqual(mid.score, 55)
        self.assertTrue(mid.anomaly)

        low = score_first_day_return(-0.02)
        self.assertEqual(low.band, BAND_LOW)
        self.assertEqual(low.score, 35)
        self.assertTrue(low.anomaly)

    def test_boundaries(self):
        self.assertEqual(score_first_day_return(0.03).band, BAND_HIGH)
        self.assertEqual(score_first_day_return(0.01).band, BAND_MID_HIGH)
        self.assertEqual(score_first_day_return(-0.01).band, BAND_LOW)
        self.assertEqual(score_first_day_return(-0.009).band, BAND_MID)

    def test_reaction_start_after_hours_rolls_next_day(self):
        # 2026-09-02 23:06 UTC = 19:06 ET 盘后 → 下一交易日起算
        pub = datetime(2026, 9, 2, 23, 6, tzinfo=timezone.utc)
        self.assertEqual(reaction_start_date(pub).isoformat(), "2026-09-03")
        # 15:00 UTC = 11:00 ET 盘中 → 当日
        pub2 = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(reaction_start_date(pub2).isoformat(), "2026-09-02")
        # 刚好 16:00 ET = 20:00 UTC → 盘后
        pub3 = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(reaction_start_date(pub3).isoformat(), "2026-09-03")


if __name__ == "__main__":
    unittest.main()
