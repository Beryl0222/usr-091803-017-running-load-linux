"""按角色裁剪视图：跑者看自己的全部细节，教练与医疗顾问各看授权摘要。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .errors import ForbiddenError, NotFoundError, ValidationError
from .models import iso, utcnow

# 回放时会被串起来的事件类型：确认、覆盖与数据修正都属于“后来的人为操作”。
_ADJUSTMENT_TYPES = ("recommendation_confirmation", "recommendation_confirmed",
                     "override_applied", "metric_corrected")


def finding_dict(finding) -> dict:
    return {"rule_id": finding.rule_id, "severity": finding.severity,
            "summary": finding.summary, "detail": finding.detail,
            "confirmer": finding.confirmer}


def plan_version_dict(version) -> dict:
    return {"version_id": version.version_id, "version_no": version.version_no,
            "created_by": version.created_by, "reason": version.reason,
            "valid_until": iso(version.valid_until), "sessions": version.sessions,
            "supersedes": version.supersedes, "created_at": iso(version.created_at)}


def event_dict(event) -> dict:
    return {"event_id": event.event_id, "ts": iso(event.ts), "actor": event.actor,
            "type": event.type, "payload": event.payload}


class AccessService:
    def __init__(self, store, recommend):
        self.store = store
        self.recommend = recommend

    # ---------- 角色判定 ----------

    def role_for(self, user_id, actor, now=None) -> str | None:
        """actor 相对该用户的角色：runner / coach / medical / None。"""
        if not actor:
            return None
        if actor == user_id:
            return "runner"
        now = now or utcnow()
        for role in ("coach", "medical"):
            if any(g.user_id == user_id and g.role == role
                   and g.grantee_id == actor
                   and (g.valid_until is None or g.valid_until > now)
                   for g in self.store.grants):
                return role
        return None

    def _require_role(self, user_id, actor, now=None) -> str:
        role = self.role_for(user_id, actor, now)
        if role is None:
            raise ForbiddenError("无权查看他人健康数据")
        return role

    # ---------- 建议视图 ----------

    def recommendation_full(self, rec) -> dict:
        """建议的完整视图，供本人动作（生成、确认）的响应使用。"""
        return self._rec_full(rec)

    def view_recommendation(self, rec_id, actor, now=None) -> dict:
        rec = self._rec(rec_id)
        role = self._require_role(rec.user_id, actor, now)
        if role == "runner":
            return {"role": role, "recommendation": self._rec_full(rec)}
        if role == "coach":
            return {"role": role, "recommendation": self._rec_coach_summary(rec)}
        return {"role": role, "recommendation": self._rec_medical_summary(rec)}

    def _rec_full(self, rec) -> dict:
        return {
            "rec_id": rec.rec_id, "user_id": rec.user_id,
            "status": rec.status, "created_at": iso(rec.created_at),
            "findings": [finding_dict(f) for f in rec.findings],
            "required_confirmations": rec.required_confirmations,
            "confirmed_by": rec.confirmed_by,
            "suggested_sessions": rec.suggested_sessions,
            "suggestion_basis": rec.suggestion_basis,
            "plan_version_id": rec.plan_version_id,
            "explanation": rec.explanation,
        }

    def _rec_coach_summary(self, rec) -> dict:
        """教练摘要：负荷与排课可见，原始心率、睡眠等健康细节不可见。"""
        data = rec.explanation["data_used"]
        confirmed = {c["role"] for c in rec.confirmed_by}
        return {
            "rec_id": rec.rec_id, "user_id": rec.user_id,
            "status": rec.status, "created_at": iso(rec.created_at),
            "current_week_load": data["current_week_load"],
            "weekly_capacity": data["baseline"]["weekly_capacity"],
            "suggested_sessions": rec.suggested_sessions,
            "suggestion_basis": rec.suggestion_basis,
            "flags": [f.rule_id for f in rec.findings],
            "pending_confirmations": [r for r in rec.required_confirmations
                                      if r not in confirmed],
        }

    def _rec_medical_summary(self, rec) -> dict:
        """医疗摘要：风险判断与伤病可见，训练排课细节不可见。"""
        injuries = [t.label for t in self.store.injuries
                    if t.user_id == rec.user_id and t.cleared_at is None]
        return {
            "rec_id": rec.rec_id, "user_id": rec.user_id,
            "status": rec.status, "created_at": iso(rec.created_at),
            "health_findings": [finding_dict(f) for f in rec.findings
                                if f.severity == "hold"],
            "required_confirmations": rec.required_confirmations,
            "confirmed_by": rec.confirmed_by,
            "active_injuries": injuries,
        }

    # ---------- 周负荷视图 ----------

    def view_week(self, user_id, actor, week=None, now=None) -> dict:
        user = self.store.users.get(user_id)
        if user is None:
            raise NotFoundError(f"用户不存在：{user_id}")
        role = self._require_role(user_id, actor, now)
        now = now or utcnow()
        zone = ZoneInfo(user.home_tz)
        if week is None:
            year, w, _ = now.astimezone(zone).isocalendar()
            week = f"{year}-W{w:02d}"
        year, w = self._parse_week(week)
        start, end = self._week_bounds(year, w, zone)

        records = [r for r in self.recommend.ingest.active_records(user_id)
                   if date.fromisoformat(r.local_date).isocalendar()[:2] == (year, w)]
        total_load = round(sum(r.load for r in records), 2)

        hr = self.recommend.ingest.wellness_samples(user_id, "heart_rate", start)
        sleep = self.recommend.ingest.wellness_samples(user_id, "sleep", start)
        fatigue = self.recommend.ingest.wellness_samples(user_id, "fatigue", start)
        in_week = lambda s: start <= s.recorded_at < end
        without_hr = [r.record_id for r in records if r.avg_hr is None]
        completeness = {
            "has_heart_rate": any(in_week(s) for s in hr)
                              or len(without_hr) < len(records),
            "has_sleep": any(in_week(s) for s in sleep),
            "has_fatigue": any(in_week(s) for s in fatigue),
            "workouts_without_hr": len(without_hr),
        }
        duplicates = self._count_events(user_id, "metrics_duplicate_ignored", start, end)
        corrections = self._count_events(user_id, "metric_corrected", start, end)
        uncertain = (not completeness["has_heart_rate"]
                     or not completeness["has_sleep"]
                     or not completeness["has_fatigue"]
                     or completeness["workouts_without_hr"] > 0)

        if role == "runner":
            return {"role": role, "week": {
                "user_id": user_id, "week": week, "total_load": total_load,
                "sessions": [
                    {"record_id": r.record_id, "local_date": r.local_date,
                     "duration_min": r.duration_min, "avg_hr": r.avg_hr,
                     "load": r.load, "source": r.source}
                    for r in sorted(records, key=lambda x: x.local_date)
                ],
                "data_completeness": completeness,
                "duplicates_ignored": duplicates,
                "corrections_applied": corrections,
                "uncertain": uncertain,
            }}
        if role == "coach":
            return {"role": role, "week": {
                "user_id": user_id, "week": week, "total_load": total_load,
                "session_count": len(records),
                "data_completeness": completeness,
                "uncertain": uncertain,
            }}
        return {"role": role, "week": {
            "user_id": user_id, "week": week, "uncertain": uncertain,
            "active_injuries": [t.label for t in self.store.injuries
                                if t.user_id == user_id and t.cleared_at is None],
        }}

    # ---------- 回放 ----------

    def replay(self, rec_id, actor, now=None) -> dict:
        """完整呈现原始记录、风险判断以及后来是否被人工调整。"""
        rec = self._rec(rec_id)
        self._require_role(rec.user_id, actor, now)

        adjustments = []
        related_versions = {rec.plan_version_id} - {None}
        for event in self.store.events:
            if event.ts < rec.created_at or event.type not in _ADJUSTMENT_TYPES:
                continue
            payload = event.payload
            if payload.get("rec_id") == rec_id:
                adjustments.append(event)
            elif payload.get("user_id") == rec.user_id and event.type in (
                    "override_applied", "metric_corrected"):
                if event.type == "override_applied":
                    # 覆盖若取代的是本建议产生的版本（或其后继），串入回放。
                    if payload.get("supersedes") in related_versions:
                        adjustments.append(event)
                        related_versions.add(payload.get("version_id"))
                else:
                    adjustments.append(event)
        manually_adjusted = any(e.type == "override_applied" for e in adjustments)

        return {
            "recommendation": self._rec_full(rec),
            "inputs": rec.explanation["data_used"],
            "rules": rec.explanation["rules_evaluated"],
            "risk_judgments": [finding_dict(f) for f in rec.findings],
            "adjustments": [event_dict(e) for e in adjustments],
            "manually_adjusted": manually_adjusted,
        }

    # ---------- 内部 ----------

    def _rec(self, rec_id):
        rec = self.store.recommendations.get(rec_id)
        if rec is None:
            raise NotFoundError(f"建议不存在：{rec_id}")
        return rec

    def _count_events(self, user_id, type_, start, end) -> int:
        return sum(1 for e in self.store.events
                   if e.type == type_ and e.payload.get("user_id") == user_id
                   and start <= e.ts < end)

    @staticmethod
    def _parse_week(week):
        try:
            year_s, week_s = week.split("-W")
            return int(year_s), int(week_s)
        except (ValueError, AttributeError):
            raise ValidationError(f"周参数格式应为 YYYY-Www：{week}")

    @staticmethod
    def _week_bounds(year, week, zone):
        monday = date.fromisocalendar(year, week, 1)
        start = datetime(monday.year, monday.month, monday.day, tzinfo=zone)
        return start, start + timedelta(days=7)
