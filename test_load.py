"""个性化负荷与恢复状态：同样的 30 分钟对不同人群不是同一种负荷。"""

import unittest

from training.load import (increase_cap_for, readiness, sensitivity_for,
                           session_intensity, session_load, window_load)
from training.models import PopulationGroup, SessionRecord
from training import ingestion

from testkit import NOW, SYSTEM, add_profile, envelope, make_store, session


def _record(avg_hr=120, duration=30.0):
    return SessionRecord(
        record_id="rec", runner_id="r1", source="t", external_id="e",
        started_at=NOW, duration_min=duration, home_date=NOW.date(),
        device_timezone="Asia/Shanghai", avg_hr=avg_hr,
    )


class PersonalizedLoadTest(unittest.TestCase):
    def test_same_30min_is_different_load_per_group(self):
        store = make_store()
        expected = {
            PopulationGroup.GENERAL: 15.0,    # 30 × 0.5 × 1.0
            PopulationGroup.BEGINNER: 18.0,   # × 1.2
            PopulationGroup.CHRONIC: 21.0,    # × 1.4
            PopulationGroup.RECOVERY: 24.0,   # × 1.6
        }
        for group, want in expected.items():
            profile = add_profile(store, runner_id=f"r-{group.value}", groups=(group,))
            self.assertAlmostEqual(session_load(profile, _record()), want)

    def test_multi_label_takes_most_conservative(self):
        groups = (PopulationGroup.GENERAL, PopulationGroup.RECOVERY)
        self.assertEqual(sensitivity_for(groups), 1.6)
        self.assertEqual(increase_cap_for(groups), 0.05)

    def test_intensity_uses_heart_rate_reserve(self):
        store = make_store()
        profile = add_profile(store)  # 静息 60，最大 180
        self.assertEqual(session_intensity(profile, _record(avg_hr=60)), 0.0)
        self.assertAlmostEqual(session_intensity(profile, _record(avg_hr=120)), 0.5)
        self.assertAlmostEqual(session_intensity(profile, _record(avg_hr=180)), 1.0)
        self.assertAlmostEqual(session_intensity(profile, _record(avg_hr=240)), 1.5)  # 截断

    def test_missing_heart_rate_means_no_load(self):
        store = make_store()
        profile = add_profile(store)
        record = _record()
        record.avg_hr = None
        self.assertIsNone(session_load(profile, record))


class ReadinessTest(unittest.TestCase):
    def test_readiness_deductions(self):
        store = make_store()
        profile = add_profile(store)
        ingestion.ingest_sync(store, envelope(
            "sync-r", "r1",
            dailies=[{"external_id": "d1", "day": "2026-09-18", "sleep_hours": 5.5,
                      "fatigue_score": 9, "morning_resting_hr": 70}],
        ), SYSTEM, now=NOW)
        score, parts = readiness(profile, store.dailies_for("r1"))
        self.assertEqual(score, 35)  # 100 - 20(睡眠) - 25(疲劳) - 20(晨起心率)
        self.assertEqual({p["factor"] for p in parts}, {"sleep", "fatigue", "resting_hr"})

    def test_well_rested_keeps_full_score(self):
        store = make_store()
        profile = add_profile(store)
        ingestion.ingest_sync(store, envelope(
            "sync-ok", "r1",
            dailies=[{"external_id": "d1", "day": "2026-09-18", "sleep_hours": 8.0,
                      "fatigue_score": 2, "morning_resting_hr": 60}],
        ), SYSTEM, now=NOW)
        score, parts = readiness(profile, store.dailies_for("r1"))
        self.assertEqual(score, 100)
        self.assertEqual(parts, [])


class WindowLoadTest(unittest.TestCase):
    def test_window_excludes_superseded_and_flags_missing(self):
        store = make_store()
        profile = add_profile(store)
        ingestion.ingest_sync(store, envelope(
            "sync-1", "r1",
            sessions=[session("s1", "2026-09-16"),
                      session("s2", "2026-09-17", avg_hr=None)],
        ), SYSTEM, now=NOW)
        # 手工修正：s1 改为 45 分钟
        ingestion.ingest_sync(store, envelope(
            "sync-2", "r1",
            sessions=[session("s1-fix", "2026-09-16", duration=45.0, corrects="s1")],
        ), SYSTEM, now=NOW)

        window = window_load(store, profile, NOW.date())
        self.assertAlmostEqual(window.total, 22.5)  # 45 × 0.5 × 1.0，只算修正后版本
        self.assertEqual(len(window.considered), 1)
        self.assertEqual([s.external_id for s in window.missing_hr], ["s2"])

    def test_window_respects_home_date_bounds(self):
        store = make_store()
        profile = add_profile(store)
        ingestion.ingest_sync(store, envelope(
            "sync-1", "r1", sessions=[session("s1", "2026-09-11")],  # 窗口外
        ), SYSTEM, now=NOW)
        window = window_load(store, profile, NOW.date())
        self.assertEqual(window.total, 0.0)
        self.assertEqual(window.session_count, 0)


if __name__ == "__main__":
    unittest.main()
