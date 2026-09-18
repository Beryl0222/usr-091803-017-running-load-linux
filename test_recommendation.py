"""建议引擎：可解释生成、风险暂停、确认恢复、覆盖留痕与回放。"""

import unittest
from datetime import date, timedelta

from training import ingestion, recommendation
from training.errors import ConflictError, ForbiddenError, ValidationError
from training.models import (Actor, PopulationGroup, RecommendationStatus,
                             RiskFlag, Role)
from training.profiles import grant_authorization

from testkit import (NOW, SYSTEM, add_profile, daily, envelope, make_store,
                     session, three_easy_sessions)

RUNNER = Actor("r1", Role.RUNNER)
COACH = Actor("c1", Role.COACH)
EXPIRES = NOW + timedelta(days=3)


def _seed(store, runner_id="r1", sessions=None, dailies=None, **profile_kw):
    add_profile(store, runner_id=runner_id, **profile_kw)
    ingestion.ingest_sync(store, envelope(
        "sync-1", runner_id,
        sessions=sessions if sessions is not None else three_easy_sessions(),
        dailies=dailies if dailies is not None else [daily("d1", "2026-09-18")],
    ), SYSTEM, now=NOW)


class GenerationTest(unittest.TestCase):
    def test_normal_generation_is_active_and_explainable(self):
        store = make_store()
        _seed(store)
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)

        self.assertEqual(rec.status, RecommendationStatus.ACTIVE)
        self.assertAlmostEqual(rec.recent_load, 45.0)          # 3 × 30min × 0.5
        self.assertAlmostEqual(rec.target_weekly_load, 49.5)   # 增幅上限 10%
        self.assertEqual(len(rec.planned_sessions), 3)
        self.assertEqual([s.day for s in rec.planned_sessions],
                         [date(2026, 9, 21), date(2026, 9, 23), date(2026, 9, 26)])
        self.assertFalse(any(s.suspended for s in rec.planned_sessions))

        # 可解释：说明采用了哪些数据与规则
        self.assertIn("heart_rate", rec.explanation["data_used"])
        self.assertIn("cadence", rec.explanation["data_used"])
        self.assertIn("sleep", rec.explanation["data_used"])
        self.assertIn("subjective_fatigue", rec.explanation["data_used"])
        self.assertIn("personal_baseline", rec.explanation["data_used"])
        rule_names = [r["rule"] for r in rec.explanation["rules"]]
        self.assertIn("personalized_load_sensitivity", rule_names)
        self.assertIn("weekly_increase_cap", rule_names)
        self.assertEqual(len(rec.input_snapshot["sessions"]), 3)

    def test_chronic_runner_has_lower_cap_and_higher_load(self):
        store = make_store()
        _seed(store, groups=(PopulationGroup.CHRONIC,))
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        self.assertAlmostEqual(rec.recent_load, 63.0)          # 3 × 30 × 0.5 × 1.4
        self.assertAlmostEqual(rec.target_weekly_load, 66.15)  # 增幅上限 5%

    def test_low_readiness_only_tapers_down(self):
        store = make_store()
        _seed(store, dailies=[daily("d1", "2026-09-18", sleep=5.5, fatigue=9)])
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        self.assertEqual(rec.status, RecommendationStatus.ACTIVE)
        self.assertAlmostEqual(rec.target_weekly_load, 39.6)   # 49.5 × 0.8
        rule_names = [r["rule"] for r in rec.explanation["rules"]]
        self.assertIn("readiness_taper", rule_names)

    def test_new_recommendation_supersedes_previous(self):
        store = make_store()
        _seed(store)
        first = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        second = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        self.assertEqual(first.status, RecommendationStatus.SUPERSEDED)
        self.assertEqual(second.supersedes, first.recommendation_id)


class RiskGateTest(unittest.TestCase):
    def _assert_paused(self, rec, flag):
        self.assertEqual(rec.status, RecommendationStatus.PAUSED)
        self.assertIn(flag, rec.risk_flags)
        self.assertIsNotNone(rec.confirmation_request)
        self.assertTrue(all(s.suspended for s in rec.planned_sessions))
        # 不自动增加训练量
        self.assertLessEqual(rec.target_weekly_load, rec.recent_load)
        rule_names = [r["rule"] for r in rec.explanation["rules"]]
        self.assertIn("risk_no_increase", rule_names)

    def test_abnormal_session_heart_rate_pauses(self):
        store = make_store()
        _seed(store, sessions=[session("s1", "2026-09-17", max_hr=170)])  # ≥ 90% × 180
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        self._assert_paused(rec, RiskFlag.ABNORMAL_HEART_RATE)

    def test_abnormal_morning_resting_hr_pauses(self):
        store = make_store()
        _seed(store, dailies=[daily("d1", "2026-09-18", morning_hr=70)])  # > 110% × 60
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        self._assert_paused(rec, RiskFlag.ABNORMAL_HEART_RATE)

    def test_active_injury_tag_pauses(self):
        store = make_store()
        _seed(store, injuries=("膝盖不适",))
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        self._assert_paused(rec, RiskFlag.ACTIVE_INJURY)

    def test_timezone_change_pauses(self):
        store = make_store()
        add_profile(store, "r1")
        ingestion.ingest_sync(store, envelope(
            "sync-1", "r1", device_timezone="America/New_York",
            sessions=three_easy_sessions(), dailies=[daily("d1", "2026-09-18")],
        ), SYSTEM, now=NOW)
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        self._assert_paused(rec, RiskFlag.TIMEZONE_CHANGE)

    def test_missing_key_data_pauses(self):
        store = make_store()
        _seed(store, sessions=[session("s1", "2026-09-17", avg_hr=None)])
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        self._assert_paused(rec, RiskFlag.MISSING_KEY_DATA)

    def test_paused_event_logged(self):
        store = make_store()
        _seed(store, injuries=("脚踝扭伤",))
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        types = [e.type for e in store.events_for_entity(rec.recommendation_id)]
        self.assertIn("recommendation_created", types)
        self.assertIn("recommendation_paused", types)


class ConfirmationTest(unittest.TestCase):
    def _paused_rec(self, store):
        _seed(store, injuries=("膝盖不适",))
        return recommendation.generate(store, "r1", SYSTEM, now=NOW)

    def test_runner_confirm_resumes_plan(self):
        store = make_store()
        rec = self._paused_rec(store)
        confirmed = recommendation.confirm(store, rec.recommendation_id, RUNNER,
                                           note="感觉良好", now=NOW)
        self.assertEqual(confirmed.status, RecommendationStatus.ACTIVE)
        self.assertFalse(any(s.suspended for s in confirmed.planned_sessions))
        self.assertIsNone(confirmed.confirmation_request)
        self.assertEqual(confirmed.confirmations[0].actor_id, "r1")
        types = [e.type for e in store.events_for_entity(rec.recommendation_id)]
        self.assertIn("recommendation_confirmed", types)

    def test_unauthorized_coach_cannot_confirm(self):
        store = make_store()
        rec = self._paused_rec(store)
        with self.assertRaises(ForbiddenError):
            recommendation.confirm(store, rec.recommendation_id, COACH, now=NOW)

    def test_authorized_coach_can_confirm(self):
        store = make_store()
        rec = self._paused_rec(store)
        grant_authorization(store, "r1", {
            "grantee_role": "coach", "grantee_id": "c1",
            "expires_at": (NOW + timedelta(days=7)).isoformat(),
        }, RUNNER, now=NOW)
        confirmed = recommendation.confirm(store, rec.recommendation_id, COACH, now=NOW)
        self.assertEqual(confirmed.status, RecommendationStatus.ACTIVE)

    def test_confirming_active_recommendation_conflicts(self):
        store = make_store()
        _seed(store)
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        with self.assertRaises(ConflictError):
            recommendation.confirm(store, rec.recommendation_id, RUNNER, now=NOW)


class OverrideTest(unittest.TestCase):
    def _active_rec_with_coach(self, store):
        _seed(store)
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        grant_authorization(store, "r1", {
            "grantee_role": "coach", "grantee_id": "c1",
            "expires_at": (NOW + timedelta(days=7)).isoformat(),
        }, RUNNER, now=NOW)
        return rec

    def test_override_keeps_reason_deadline_and_original(self):
        store = make_store()
        rec = self._active_rec_with_coach(store)
        updated = recommendation.override(
            store, rec.recommendation_id, COACH,
            reason="跑者反馈睡眠不足，减量一周",
            expires_at=EXPIRES,
            changes={"target_weekly_load": 40.0}, now=NOW)

        self.assertEqual(updated.version, 2)
        self.assertAlmostEqual(updated.target_weekly_load, 40.0)
        self.assertEqual(len(updated.overrides), 1)
        override = updated.overrides[0]
        self.assertEqual(override.reason, "跑者反馈睡眠不足，减量一周")
        self.assertEqual(override.expires_at, EXPIRES)
        self.assertAlmostEqual(override.original["target_weekly_load"], 49.5)  # 原版本
        self.assertEqual(override.version_before, 1)
        types = [e.type for e in store.events_for_entity(rec.recommendation_id)]
        self.assertIn("override_applied", types)

    def test_override_requires_reason_and_future_deadline(self):
        store = make_store()
        rec = self._active_rec_with_coach(store)
        with self.assertRaises(ValidationError):
            recommendation.override(store, rec.recommendation_id, COACH,
                                    reason="  ", expires_at=EXPIRES,
                                    changes={"target_weekly_load": 40}, now=NOW)
        with self.assertRaises(ValidationError):
            recommendation.override(store, rec.recommendation_id, COACH,
                                    reason="减量", expires_at=NOW - timedelta(days=1),
                                    changes={"target_weekly_load": 40}, now=NOW)
        with self.assertRaises(ValidationError):
            recommendation.override(store, rec.recommendation_id, COACH,
                                    reason="减量", expires_at=EXPIRES,
                                    changes={"unknown_field": 1}, now=NOW)

    def test_override_requires_professional_with_grant(self):
        store = make_store()
        _seed(store)
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        with self.assertRaises(ForbiddenError):
            recommendation.override(store, rec.recommendation_id, RUNNER,
                                    reason="x", expires_at=EXPIRES,
                                    changes={"target_weekly_load": 40}, now=NOW)
        with self.assertRaises(ForbiddenError):  # 教练无授权
            recommendation.override(store, rec.recommendation_id, COACH,
                                    reason="x", expires_at=EXPIRES,
                                    changes={"target_weekly_load": 40}, now=NOW)


class ReplayTest(unittest.TestCase):
    def test_replay_shows_inputs_risk_and_human_adjustments(self):
        store = make_store()
        _seed(store, injuries=("膝盖不适",))
        rec = recommendation.generate(store, "r1", SYSTEM, now=NOW)
        recommendation.confirm(store, rec.recommendation_id, RUNNER, now=NOW)
        grant_authorization(store, "r1", {
            "grantee_role": "coach", "grantee_id": "c1",
            "expires_at": (NOW + timedelta(days=7)).isoformat(),
        }, RUNNER, now=NOW)
        recommendation.override(store, rec.recommendation_id, COACH,
                                reason="复查后允许低强度", expires_at=EXPIRES,
                                changes={"target_weekly_load": 30.0}, now=NOW)

        replay = recommendation.replay(store, rec.recommendation_id, now=NOW)
        # 原始记录完整呈现
        self.assertEqual(len(replay["input_snapshot"]["sessions"]), 3)
        self.assertEqual(replay["input_snapshot"]["profile"]["resting_hr"], 60)
        # 风险判断
        self.assertIn("active_injury", replay["risk_assessment"]["flags"])
        # 后来是否被人工调整
        self.assertTrue(replay["adjusted_by_human"])
        kinds = [a["type"] for a in replay["human_adjustments"]]
        self.assertEqual(kinds, ["confirmation", "override"])
        override_adj = replay["human_adjustments"][1]
        self.assertEqual(override_adj["reason"], "复查后允许低强度")
        self.assertAlmostEqual(override_adj["original"]["target_weekly_load"], 45.0)
        # 事件链完整
        types = {e["type"] for e in replay["events"]}
        self.assertTrue({"recommendation_created", "recommendation_paused",
                         "recommendation_confirmed", "override_applied"} <= types)


if __name__ == "__main__":
    unittest.main()
