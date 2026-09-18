"""领域行为测试：负荷、去重、安全暂停、确认、覆盖、访问控制与回放。"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from domain import ForbiddenError, TrainingService, ValidationError
from domain.errors import ConflictError

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)  # 周五，上海 20:00，第 38 周


def make_service():
    return TrainingService()


def add_user(svc, uid="u1", group="beginner", home_tz="Asia/Shanghai",
             resting_hr=60, max_hr=180):
    return svc.directory.create_user(uid, group, resting_hr, max_hr, home_tz, now=NOW)


def workout_sample(hours_ago=24, avg_hr=120, duration_min=30, recorded_at=None):
    return {"kind": "workout",
            "recorded_at": recorded_at or (NOW - timedelta(hours=hours_ago)).isoformat(),
            "value": {"duration_min": duration_min, "avg_hr": avg_hr,
                      "avg_cadence": 170, "distance_km": 2.5}}


def feed_full_data(svc, uid, sync_id="s1", avg_hr=120, tz="Asia/Shanghai"):
    """喂入训练、睡眠与主观疲劳，使建议满足数据完整性前提。"""
    return svc.ingest.ingest(uid, sync_id, tz=tz, now=NOW, samples=[
        workout_sample(avg_hr=avg_hr),
        {"kind": "sleep", "recorded_at": (NOW - timedelta(hours=10)).isoformat(),
         "value": {"hours": 7.5}},
        {"kind": "fatigue", "recorded_at": (NOW - timedelta(hours=5)).isoformat(),
         "value": {"rpe": 3, "note": "轻松"}},
    ])


class LoadTest(unittest.TestCase):
    def test_same_duration_loads_differ_by_group(self):
        svc = make_service()
        add_user(svc, "u_beginner", group="beginner")
        add_user(svc, "u_recovering", group="recovering")
        for uid in ("u_beginner", "u_recovering"):
            svc.ingest.ingest(uid, "s1", tz="Asia/Shanghai", now=NOW,
                              samples=[workout_sample()])
        beginner = svc.ingest.active_records("u_beginner")[0].load
        recovering = svc.ingest.active_records("u_recovering")[0].load
        # 同样的 30 分钟超慢跑：初学者 24.0，伤后恢复者 36.0。
        self.assertAlmostEqual(beginner, 24.0)
        self.assertAlmostEqual(recovering, 36.0)
        self.assertAlmostEqual(recovering / beginner, 1.5)

    def test_duplicate_sync_counts_load_once(self):
        svc = make_service()
        add_user(svc)
        first = svc.ingest.ingest("u1", "s1", tz="Asia/Shanghai", now=NOW,
                                  samples=[workout_sample()])
        second = svc.ingest.ingest("u1", "s1", tz="Asia/Shanghai", now=NOW,
                                   samples=[workout_sample()])
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "duplicate")
        user = svc.store.users["u1"]
        self.assertAlmostEqual(svc.recommend.week_load(user, NOW), 24.0)
        self.assertTrue(any(e.type == "metrics_duplicate_ignored"
                            for e in svc.store.events))

    def test_cross_day_sync_attributes_to_recording_day(self):
        svc = make_service()
        add_user(svc)
        # 周日 23:50（第 37 周）训练，周一（第 38 周）才同步，应归因到训练发生日。
        svc.ingest.ingest("u1", "s1", tz="Asia/Shanghai", now=NOW, samples=[
            workout_sample(recorded_at="2026-09-13T23:50:00+08:00")])
        record = svc.ingest.active_records("u1")[0]
        self.assertEqual(record.local_date, "2026-09-13")
        week37 = svc.access.view_week("u1", "u1", week="2026-W37", now=NOW)["week"]
        week38 = svc.access.view_week("u1", "u1", week="2026-W38", now=NOW)["week"]
        self.assertAlmostEqual(week37["total_load"], 24.0)
        self.assertEqual(week38["total_load"], 0)

    def test_manual_correction_replaces_without_double_count(self):
        svc = make_service()
        add_user(svc)
        svc.ingest.ingest("u1", "s1", tz="Asia/Shanghai", now=NOW,
                          samples=[workout_sample(duration_min=30)])
        result = svc.ingest.ingest("u1", "s2", tz="Asia/Shanghai", source="manual",
                                   corrects="s1", now=NOW,
                                   samples=[workout_sample(duration_min=45)])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(result["corrected_records"]), 1)
        user = svc.store.users["u1"]
        # 修正后只保留 45 分钟的一次负荷，而不是 30 + 45。
        self.assertAlmostEqual(svc.recommend.week_load(user, NOW), 36.0)
        original = [r for r in svc.store.records.values() if r.sync_id == "s1"][0]
        self.assertEqual(original.status, "superseded")
        self.assertEqual(original.corrected_by, "s2")
        self.assertTrue(any(e.type == "metric_corrected" for e in svc.store.events))


class SafetyHoldTest(unittest.TestCase):
    def test_abnormal_hr_holds_until_medical_confirms(self):
        svc = make_service()
        add_user(svc)
        feed_full_data(svc, "u1", avg_hr=160)  # 0.85 * 180 = 153，超出即异常
        rec = svc.recommend.generate("u1", now=NOW)
        self.assertEqual(rec.status, "held")
        self.assertEqual(rec.required_confirmations, ["medical"])
        self.assertIn("abnormal-heart-rate",
                      [f.rule_id for f in rec.findings])
        # 暂停相关安排：不产生新计划版本，也不增加训练量。
        self.assertEqual(svc.store.plan_status["u1"]["status"], "paused")
        self.assertEqual(svc.store.plan_versions.get("u1"), None)
        self.assertIsNone(rec.plan_version_id)

        with self.assertRaises(ValidationError):
            svc.recommend.confirm(rec.rec_id, "u1", "runner", now=NOW)
        with self.assertRaises(ForbiddenError):
            svc.recommend.confirm(rec.rec_id, "dr_li", "medical", now=NOW)

        svc.directory.grant("u1", actor="u1", role="medical",
                            grantee_id="dr_li", now=NOW)
        rec = svc.recommend.confirm(rec.rec_id, "dr_li", "medical",
                                    note="心电图复查无异常", now=NOW)
        self.assertEqual(rec.status, "confirmed")
        self.assertEqual(svc.store.plan_status["u1"]["status"], "active")
        self.assertIsNotNone(rec.plan_version_id)
        version = svc.recommend.current_version("u1", NOW)
        self.assertEqual(version.version_id, rec.plan_version_id)
        self.assertEqual(version.sessions, rec.suggested_sessions)
        self.assertTrue(any(e.type == "recommendation_confirmed"
                            for e in svc.store.events))

    def test_active_injury_holds_and_clears(self):
        svc = make_service()
        add_user(svc)
        feed_full_data(svc, "u1")
        svc.directory.add_injury("u1", "膝盖不适", actor="u1", now=NOW)
        rec = svc.recommend.generate("u1", now=NOW)
        self.assertEqual(rec.status, "held")
        self.assertEqual(rec.required_confirmations, ["medical"])
        self.assertIn("active-injury", [f.rule_id for f in rec.findings])

        svc.directory.clear_injury("u1", "膝盖不适", actor="dr_li", now=NOW)
        rec2 = svc.recommend.generate("u1", now=NOW)
        self.assertEqual(rec2.status, "ready")

    def test_timezone_change_holds_and_runner_confirm_adopts_tz(self):
        svc = make_service()
        add_user(svc)
        feed_full_data(svc, "u1", tz="Europe/Berlin")
        rec = svc.recommend.generate("u1", now=NOW)
        self.assertEqual(rec.status, "held")
        self.assertEqual(rec.required_confirmations, ["runner"])
        self.assertIn("timezone-change", [f.rule_id for f in rec.findings])

        with self.assertRaises(ForbiddenError):
            svc.recommend.confirm(rec.rec_id, "someone_else", "runner", now=NOW)
        rec = svc.recommend.confirm(rec.rec_id, "u1", "runner",
                                    note="出差到柏林", now=NOW)
        self.assertEqual(rec.status, "confirmed")
        user = svc.store.users["u1"]
        self.assertEqual(user.home_tz, "Europe/Berlin")
        self.assertIsNone(user.last_seen_tz)
        self.assertTrue(any(e.type == "timezone_updated" for e in svc.store.events))

    def test_missing_key_data_holds(self):
        svc = make_service()
        add_user(svc)
        svc.ingest.ingest("u1", "s1", tz="Asia/Shanghai", now=NOW,
                          samples=[workout_sample()])  # 只有训练，没有睡眠与疲劳
        rec = svc.recommend.generate("u1", now=NOW)
        self.assertEqual(rec.status, "held")
        self.assertEqual(rec.required_confirmations, ["runner"])
        finding = [f for f in rec.findings if f.rule_id == "missing-key-data"][0]
        self.assertTrue(any("睡眠" in m for m in finding.detail["missing"]))
        self.assertTrue(any("疲劳" in m for m in finding.detail["missing"]))

    def test_held_recommendation_never_increases_load(self):
        svc = make_service()
        add_user(svc)
        svc.directory.add_injury("u1", "脚踝扭伤", actor="u1", now=NOW)
        feed_full_data(svc, "u1")
        rec = svc.recommend.generate("u1", now=NOW)
        self.assertEqual(rec.status, "held")
        # 维持原安排（新用户为基础课表），且确认前不落地任何新版本。
        self.assertTrue(all(s["duration_min"] == 30 for s in rec.suggested_sessions))
        self.assertEqual(svc.store.plan_versions.get("u1"), None)

    def test_confirm_on_ready_recommendation_conflicts(self):
        svc = make_service()
        add_user(svc)
        feed_full_data(svc, "u1")
        rec = svc.recommend.generate("u1", now=NOW)
        self.assertEqual(rec.status, "ready")
        with self.assertRaises(ConflictError):
            svc.recommend.confirm(rec.rec_id, "u1", "runner", now=NOW)


class ExplainabilityTest(unittest.TestCase):
    def test_ready_recommendation_explains_data_and_rules(self):
        svc = make_service()
        add_user(svc)
        feed_full_data(svc, "u1")
        rec = svc.recommend.generate("u1", now=NOW)
        self.assertEqual(rec.status, "ready")
        data = rec.explanation["data_used"]
        self.assertEqual(data["baseline"]["group"], "beginner")
        self.assertEqual(data["baseline"]["load_multiplier"], 1.2)
        self.assertEqual(len(data["workouts"]), 1)
        self.assertEqual(data["sleep_entries"], 1)
        self.assertEqual(data["fatigue_entries"], 1)
        rules = rec.explanation["rules_evaluated"]
        self.assertEqual(len(rules), 4)
        self.assertTrue(all(r["outcome"] == "pass" for r in rules))
        self.assertIsNotNone(rec.plan_version_id)


class OverrideTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        add_user(self.svc)
        feed_full_data(self.svc, "u1")
        self.rec = self.svc.recommend.generate("u1", now=NOW)
        self.svc.directory.grant("u1", actor="u1", role="coach",
                                 grantee_id="coach_wang", now=NOW)

    def test_override_keeps_reason_expiry_and_original_version(self):
        original = self.svc.recommend.current_version("u1", NOW)
        later = NOW + timedelta(days=3)
        version = self.svc.recommend.apply_override(
            "u1", actor="coach_wang", reason="赛前减量周",
            valid_until=later.isoformat(), now=NOW,
            sessions=[{"weekday": 3, "kind": "超慢跑", "duration_min": 20,
                       "intensity": 0.55, "pace_kmh": "4-6"}])
        self.assertEqual(version.created_by, "coach_wang")
        self.assertEqual(version.reason, "赛前减量周")
        self.assertEqual(version.supersedes, original.version_id)
        # 原版本完整保留，覆盖期内生效的是覆盖版本。
        versions = self.svc.store.plan_versions["u1"]
        self.assertEqual([v.version_id for v in versions],
                         [original.version_id, version.version_id])
        self.assertEqual(self.svc.recommend.current_version("u1", NOW).version_id,
                         version.version_id)
        # 覆盖过期后自动回落到原版本。
        after_expiry = NOW + timedelta(days=4)
        self.assertEqual(
            self.svc.recommend.current_version("u1", after_expiry).version_id,
            original.version_id)

    def test_override_requires_reason_expiry_and_coach_grant(self):
        with self.assertRaises(ForbiddenError):
            self.svc.recommend.apply_override(
                "u1", actor="random", reason="x", valid_until=NOW.isoformat(),
                sessions=[{"duration_min": 20}], now=NOW)
        with self.assertRaises(ValidationError):
            self.svc.recommend.apply_override(
                "u1", actor="coach_wang", reason="  ",
                valid_until=(NOW + timedelta(days=1)).isoformat(),
                sessions=[{"duration_min": 20}], now=NOW)
        with self.assertRaises(ValidationError):
            self.svc.recommend.apply_override(
                "u1", actor="coach_wang", reason="减量", valid_until=None,
                sessions=[{"duration_min": 20}], now=NOW)
        with self.assertRaises(ValidationError):
            self.svc.recommend.apply_override(
                "u1", actor="coach_wang", reason="减量",
                valid_until=(NOW - timedelta(days=1)).isoformat(),
                sessions=[{"duration_min": 20}], now=NOW)


class AccessControlTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        add_user(self.svc)
        add_user(self.svc, "u2")
        feed_full_data(self.svc, "u1", avg_hr=160)  # 制造一条带风险判断的建议
        self.rec = self.svc.recommend.generate("u1", now=NOW)
        self.svc.directory.grant("u1", actor="u1", role="coach",
                                 grantee_id="coach_wang", now=NOW)
        self.svc.directory.grant("u1", actor="u1", role="medical",
                                 grantee_id="dr_li", now=NOW)

    def test_runner_sees_full_detail_others_are_scoped(self):
        full = self.svc.access.view_recommendation(self.rec.rec_id, "u1", NOW)
        self.assertEqual(full["role"], "runner")
        self.assertIn("explanation", full["recommendation"])
        self.assertIn("avg_hr", json.dumps(full, ensure_ascii=False))

        coach = self.svc.access.view_recommendation(self.rec.rec_id, "coach_wang", NOW)
        self.assertEqual(coach["role"], "coach")
        summary = coach["recommendation"]
        self.assertIn("current_week_load", summary)
        self.assertIn("abnormal-heart-rate", summary["flags"])
        self.assertNotIn("avg_hr", json.dumps(summary, ensure_ascii=False))
        self.assertNotIn("explanation", summary)

        medical = self.svc.access.view_recommendation(self.rec.rec_id, "dr_li", NOW)
        self.assertEqual(medical["role"], "medical")
        med_summary = medical["recommendation"]
        self.assertEqual(med_summary["health_findings"][0]["rule_id"],
                         "abnormal-heart-rate")
        self.assertNotIn("suggested_sessions", med_summary)

    def test_no_grant_no_access(self):
        with self.assertRaises(ForbiddenError):
            self.svc.access.view_recommendation(self.rec.rec_id, "u2", NOW)
        with self.assertRaises(ForbiddenError):
            self.svc.access.view_recommendation(self.rec.rec_id, "stranger", NOW)
        with self.assertRaises(ForbiddenError):
            self.svc.access.view_week("u1", "u2", now=NOW)

    def test_grant_requires_owner(self):
        with self.assertRaises(ForbiddenError):
            self.svc.directory.grant("u1", actor="coach_wang", role="coach",
                                     grantee_id="coach_wang", now=NOW)

    def test_week_views_are_scoped_by_role(self):
        runner = self.svc.access.view_week("u1", "u1", now=NOW)["week"]
        self.assertIn("sessions", runner)
        self.assertIn("data_completeness", runner)
        coach = self.svc.access.view_week("u1", "coach_wang", now=NOW)["week"]
        self.assertIn("total_load", coach)
        self.assertNotIn("sessions", coach)
        medical = self.svc.access.view_week("u1", "dr_li", now=NOW)["week"]
        self.assertIn("uncertain", medical)
        self.assertNotIn("total_load", medical)


class ReplayTest(unittest.TestCase):
    def test_replay_presents_records_rules_and_manual_adjustments(self):
        svc = make_service()
        add_user(svc)
        feed_full_data(svc, "u1")
        rec = svc.recommend.generate("u1", now=NOW)

        replay = svc.access.replay(rec.rec_id, "u1", NOW)
        self.assertEqual(len(replay["inputs"]["workouts"]), 1)
        self.assertEqual(len(replay["rules"]), 4)
        self.assertEqual(replay["risk_judgments"], [])
        self.assertEqual(replay["adjustments"], [])
        self.assertFalse(replay["manually_adjusted"])

        svc.directory.grant("u1", actor="u1", role="coach",
                            grantee_id="coach_wang", now=NOW)
        svc.recommend.apply_override(
            "u1", actor="coach_wang", reason="天气预警，改室内",
            valid_until=(NOW + timedelta(days=2)).isoformat(), now=NOW,
            sessions=[{"weekday": 4, "kind": "超慢跑", "duration_min": 25,
                       "intensity": 0.55, "pace_kmh": "4-6"}])
        replay = svc.access.replay(rec.rec_id, "u1", NOW)
        self.assertTrue(replay["manually_adjusted"])
        overrides = [e for e in replay["adjustments"]
                     if e["type"] == "override_applied"]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["payload"]["reason"], "天气预警，改室内")
        self.assertEqual(overrides[0]["payload"]["supersedes"], rec.plan_version_id)

    def test_replay_requires_authorization(self):
        svc = make_service()
        add_user(svc)
        feed_full_data(svc, "u1")
        rec = svc.recommend.generate("u1", now=NOW)
        with self.assertRaises(ForbiddenError):
            svc.access.replay(rec.rec_id, "stranger", NOW)


class WeekSummaryTest(unittest.TestCase):
    def test_week_view_marks_uncertainty_and_counts_anomalies(self):
        svc = make_service()
        add_user(svc)
        svc.ingest.ingest("u1", "s1", tz="Asia/Shanghai", now=NOW,
                          samples=[workout_sample()])  # 缺睡眠与疲劳
        svc.ingest.ingest("u1", "s1", tz="Asia/Shanghai", now=NOW,
                          samples=[workout_sample()])  # 重复同步
        svc.ingest.ingest("u1", "s2", tz="Asia/Shanghai", source="manual",
                          corrects="s1", now=NOW,
                          samples=[workout_sample(duration_min=40)])
        week = svc.access.view_week("u1", "u1", now=NOW)["week"]
        self.assertTrue(week["uncertain"])
        self.assertFalse(week["data_completeness"]["has_sleep"])
        self.assertFalse(week["data_completeness"]["has_fatigue"])
        self.assertEqual(week["duplicates_ignored"], 1)
        self.assertEqual(week["corrections_applied"], 1)
        self.assertAlmostEqual(week["total_load"], 32.0)  # 修正后的 40 分钟只计一次


if __name__ == "__main__":
    unittest.main()
