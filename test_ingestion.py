"""设备数据摄入：重复同步只计一次负荷，手工修正保留原版本，跨日归回真实发生日。"""

import unittest
from datetime import date

from training import ingestion
from training.errors import NotFoundError, ValidationError
from training.load import window_load

from testkit import NOW, SYSTEM, add_profile, envelope, make_store, session


class DuplicateSyncTest(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        add_profile(self.store)

    def test_same_sync_package_counts_load_once(self):
        env = envelope("sync-1", "r1", sessions=[session("s1", "2026-09-17")])
        first = ingestion.ingest_sync(self.store, env, SYSTEM, now=NOW)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["ingested"], 1)

        second = ingestion.ingest_sync(self.store, env, SYSTEM, now=NOW)
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["ingested"], 0)

        self.assertEqual(len(self.store.sessions_for("r1")), 1)
        self.assertAlmostEqual(window_load(self.store, self.store.get_runner("r1"),
                                           NOW.date()).total, 15.0)
        event_types = [e.type for e in self.store.events]
        self.assertIn("sync_duplicate_ignored", event_types)

    def test_same_record_in_later_sync_is_deduped(self):
        ingestion.ingest_sync(self.store, envelope(
            "sync-1", "r1", sessions=[session("s1", "2026-09-16")]), SYSTEM, now=NOW)
        result = ingestion.ingest_sync(self.store, envelope(
            "sync-2", "r1",
            sessions=[session("s1", "2026-09-16"), session("s2", "2026-09-17")],
        ), SYSTEM, now=NOW)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(len(self.store.sessions_for("r1")), 2)
        self.assertAlmostEqual(window_load(self.store, self.store.get_runner("r1"),
                                           NOW.date()).total, 30.0)


class ManualCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        add_profile(self.store)

    def test_correction_supersedes_and_keeps_original(self):
        ingestion.ingest_sync(self.store, envelope(
            "sync-1", "r1", sessions=[session("s1", "2026-09-16", duration=30.0)],
        ), SYSTEM, now=NOW)
        result = ingestion.ingest_sync(self.store, envelope(
            "sync-2", "r1",
            sessions=[session("s1-fix", "2026-09-16", duration=45.0, corrects="s1")],
        ), SYSTEM, now=NOW)
        self.assertEqual(result["corrected"], 1)

        active = self.store.sessions_for("r1")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].duration_min, 45.0)

        all_records = self.store.sessions_for("r1", active_only=False)
        self.assertEqual(len(all_records), 2)  # 原版本保留
        original = next(r for r in all_records if r.external_id == "s1")
        self.assertIsNotNone(original.superseded_by)

        # 周负荷按修正后版本计算，不重复计数
        self.assertAlmostEqual(window_load(self.store, self.store.get_runner("r1"),
                                           NOW.date()).total, 22.5)
        self.assertIn("record_corrected", [e.type for e in self.store.events])

    def test_correction_of_unknown_record_rejected(self):
        with self.assertRaises(ValidationError):
            ingestion.ingest_sync(self.store, envelope(
                "sync-1", "r1",
                sessions=[session("s9", "2026-09-16", corrects="ghost")],
            ), SYSTEM, now=NOW)


class CrossDayAttributionTest(unittest.TestCase):
    def test_session_attributed_to_home_timezone_date(self):
        store = make_store()
        profile = add_profile(store)
        # 东京时间 9/15 00:30 = 上海时间 9/14 23:30
        result = ingestion.ingest_sync(store, envelope(
            "sync-1", "r1", device_timezone="Asia/Tokyo",
            sessions=[session("s1", "2026-09-15", start="00:30:00", offset="+09:00")],
        ), SYSTEM, now=NOW)

        self.assertEqual(result["cross_day"], [{
            "external_id": "s1",
            "device_date": "2026-09-15",
            "home_date": "2026-09-14",
        }])
        record = store.sessions_for("r1")[0]
        self.assertEqual(record.home_date, date(2026, 9, 14))

        # 负荷归到 9/14 所在的窗，而不是设备日期或到达日
        self.assertAlmostEqual(
            window_load(store, profile, date(2026, 9, 14), days=1).total, 15.0)
        self.assertEqual(
            window_load(store, profile, date(2026, 9, 13), days=1).total, 0.0)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        add_profile(self.store)

    def test_naive_timestamp_rejected(self):
        bad = envelope("sync-1", "r1", sessions=[{
            "external_id": "s1", "started_at": "2026-09-17T07:00:00",
            "duration_min": 30,
        }])
        with self.assertRaises(ValidationError):
            ingestion.ingest_sync(self.store, bad, SYSTEM, now=NOW)

    def test_unknown_runner_rejected(self):
        with self.assertRaises(NotFoundError):
            ingestion.ingest_sync(self.store, envelope("sync-1", "ghost"), SYSTEM, now=NOW)

    def test_fatigue_out_of_range_rejected(self):
        bad = envelope("sync-1", "r1", dailies=[{
            "external_id": "d1", "day": "2026-09-18", "fatigue_score": 15,
        }])
        with self.assertRaises(ValidationError):
            ingestion.ingest_sync(self.store, bad, SYSTEM, now=NOW)


if __name__ == "__main__":
    unittest.main()
