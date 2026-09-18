"""访问控制：跑者看本人完整细节，教练/医疗凭授权各看摘要。"""

import unittest
from datetime import timedelta

from training import access, ingestion, recommendation
from training.errors import ForbiddenError
from training.models import Actor, Role
from training.profiles import grant_authorization

from testkit import NOW, SYSTEM, add_profile, daily, envelope, make_store, three_easy_sessions

RUNNER1 = Actor("r1", Role.RUNNER)
RUNNER2 = Actor("r2", Role.RUNNER)
COACH = Actor("c1", Role.COACH)
MEDIC = Actor("m1", Role.MEDICAL)


def _seed_rec(store, runner_id="r1", **profile_kw):
    add_profile(store, runner_id, **profile_kw)
    ingestion.ingest_sync(store, envelope(
        "sync-1", runner_id, sessions=three_easy_sessions(),
        dailies=[daily("d1", "2026-09-18")],
    ), SYSTEM, now=NOW)
    return recommendation.generate(store, runner_id, SYSTEM, now=NOW)


def _grant(store, runner_id, role, grantee_id, days=7):
    return grant_authorization(store, runner_id, {
        "grantee_role": role, "grantee_id": grantee_id,
        "expires_at": (NOW + timedelta(days=days)).isoformat(),
    }, Actor(runner_id, Role.RUNNER), now=NOW)


class ViewKindTest(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        add_profile(self.store, "r1")
        add_profile(self.store, "r2")

    def test_runner_sees_only_self(self):
        self.assertEqual(access.view_kind(self.store, RUNNER1, "r1", now=NOW), access.FULL)
        with self.assertRaises(ForbiddenError):
            access.view_kind(self.store, RUNNER1, "r2", now=NOW)

    def test_system_sees_full(self):
        self.assertEqual(access.view_kind(self.store, SYSTEM, "r1", now=NOW), access.FULL)

    def test_coach_needs_valid_grant(self):
        with self.assertRaises(ForbiddenError):
            access.view_kind(self.store, COACH, "r1", now=NOW)
        _grant(self.store, "r1", "coach", "c1")
        self.assertEqual(access.view_kind(self.store, COACH, "r1", now=NOW),
                         access.COACH_SUMMARY)

    def test_expired_grant_is_rejected(self):
        _grant(self.store, "r1", "medical", "m1", days=7)
        later = NOW + timedelta(days=8)
        with self.assertRaises(ForbiddenError):
            access.view_kind(self.store, MEDIC, "r1", now=later)


class ProjectionTest(unittest.TestCase):
    def test_recommendation_projections(self):
        store = make_store()
        rec = _seed_rec(store, injuries=("膝盖不适",))

        full = access.project_recommendation(rec, access.FULL, now=NOW)
        self.assertIn("input_snapshot", full)
        self.assertIn("risk_details", full)
        self.assertIn("explanation", full)

        coach = access.project_recommendation(rec, access.COACH_SUMMARY, now=NOW)
        self.assertNotIn("input_snapshot", coach)      # 不看原始健康记录
        self.assertNotIn("risk_details", coach)
        self.assertIn("active_injury", coach["risk_flags"])  # 但能看到风险标记
        self.assertIn("planned_sessions", coach)             # 与训练计划
        self.assertIn("explanation_rules", coach)

        medical = access.project_recommendation(rec, access.MEDICAL_SUMMARY, now=NOW)
        self.assertNotIn("input_snapshot", medical)
        self.assertIn("risk_details", medical)         # 医疗可看风险判断细节

    def test_runner_profile_projections(self):
        store = make_store()
        profile = add_profile(store, "r1", injuries=("膝盖不适",))

        full = access.project_runner(profile, access.FULL)
        self.assertEqual(full["resting_hr"], 60)
        self.assertIn("authorizations", full)

        coach = access.project_runner(profile, access.COACH_SUMMARY)
        self.assertNotIn("resting_hr", coach)          # 教练不看健康细节
        self.assertTrue(coach["active_injury"])

        medical = access.project_runner(profile, access.MEDICAL_SUMMARY)
        self.assertEqual(medical["resting_hr"], 60)
        self.assertEqual(medical["injury_tags"][0]["label"], "膝盖不适")


class ReplayProjectionTest(unittest.TestCase):
    def test_replay_is_complete_but_scoped_by_role(self):
        store = make_store()
        rec = _seed_rec(store, injuries=("膝盖不适",))
        recommendation.confirm(store, rec.recommendation_id, RUNNER1, now=NOW)
        raw = recommendation.replay(store, rec.recommendation_id, now=NOW)

        full = access.project_replay(raw, access.FULL)
        self.assertEqual(len(full["input_snapshot"]["sessions"]), 3)
        self.assertTrue(full["adjusted_by_human"])

        coach = access.project_replay(raw, access.COACH_SUMMARY)
        self.assertNotIn("input_snapshot", coach)
        self.assertEqual(coach["input_summary"]["session_count"], 3)
        self.assertNotIn("sessions", coach["input_summary"])  # 无逐条心率记录
        self.assertTrue(coach["adjusted_by_human"])           # 调整历史仍可见

        medical = access.project_replay(raw, access.MEDICAL_SUMMARY)
        sessions = medical["input_summary"]["sessions"]
        self.assertEqual(sessions[0]["avg_hr"], 120)          # 医疗可看心率统计
        self.assertTrue(medical["adjusted_by_human"])


if __name__ == "__main__":
    unittest.main()
