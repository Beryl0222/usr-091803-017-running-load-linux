"""建议生成、确认与教练覆盖：每次建议都能说明采用了哪些数据与规则。"""

from __future__ import annotations

import math
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from .models import (
    DEFAULT_SLOW_JOG_INTENSITY,
    GROUP_LABELS,
    GROUP_LOAD_MULTIPLIER,
    PlanVersion,
    Recommendation,
    iso,
    parse_ts,
    utcnow,
)
from .rules import WELLNESS_WINDOW_DAYS, WORKOUT_WINDOW_DAYS, evaluate_rules

BASE_SESSION_MIN = 30
WEEKLY_PROGRESSION_CAP = 1.1  # 每周负荷增幅上限
SESSION_WEEKDAYS = (2, 4, 6, 1, 3, 5, 7)  # 建议排课的优先星期


class RecommendationService:
    def __init__(self, store, ingest):
        self.store = store
        self.ingest = ingest

    # ---------- 建议生成 ----------

    def generate(self, user_id, now=None) -> Recommendation:
        now = now or utcnow()
        with self.store.lock:
            user = self._user(user_id)
            workouts = [w for w in self.ingest.active_records(user_id)
                        if w.recorded_at >= now - timedelta(days=WORKOUT_WINDOW_DAYS)]
            since = now - timedelta(days=WELLNESS_WINDOW_DAYS)
            hr = self.ingest.wellness_samples(user_id, "heart_rate", since)
            sleep = self.ingest.wellness_samples(user_id, "sleep", since)
            fatigue = self.ingest.wellness_samples(user_id, "fatigue", since)
            injuries = [t for t in self.store.injuries
                        if t.user_id == user_id and t.cleared_at is None]

            findings, rules_log = evaluate_rules(
                user, workouts, hr, sleep, fatigue, injuries, now)
            holds = [f for f in findings if f.severity == "hold"]
            required = sorted({f.confirmer for f in holds if f.confirmer})

            week_load = self.week_load(user, now)
            current = self.current_version(user_id, now)
            if holds:
                # 出现异常信号：不自动增加训练量，维持原安排并暂停，等待确认。
                sessions = [dict(s) for s in current.sessions] if current \
                    else self._base_sessions(user)
                basis = "存在风险信号，维持原安排；确认前不增加训练量"
                status = "held"
            else:
                base = self._base_week_load(user)
                target = min(max(week_load, base) * WEEKLY_PROGRESSION_CAP,
                             user.weekly_capacity)
                sessions = self._sessions_for(user, target)
                basis = "数据完整，按每周增幅上限与个人能力上限给出建议"
                status = "ready"

            explanation = {
                "data_used": {
                    "workouts": [
                        {"record_id": w.record_id, "local_date": w.local_date,
                         "duration_min": w.duration_min, "avg_hr": w.avg_hr,
                         "load": w.load, "source": w.source}
                        for w in workouts
                    ],
                    "batches": sorted({w.sync_id for w in workouts}),
                    "sleep_entries": len(sleep),
                    "fatigue_entries": len(fatigue),
                    "baseline": {
                        "group": user.group,
                        "group_label": GROUP_LABELS[user.group],
                        "load_multiplier": GROUP_LOAD_MULTIPLIER[user.group],
                        "resting_hr": user.resting_hr,
                        "max_hr": user.max_hr,
                        "weekly_capacity": user.weekly_capacity,
                    },
                    "current_week_load": week_load,
                },
                "rules_evaluated": rules_log,
            }

            rec = Recommendation(
                rec_id=self.store.new_id("recmd"),
                user_id=user_id,
                created_at=now,
                status=status,
                findings=findings,
                required_confirmations=required,
                explanation=explanation,
                suggested_sessions=sessions,
                suggestion_basis=basis,
            )
            self.store.recommendations[rec.rec_id] = rec

            if holds:
                self.store.plan_status[user_id] = {"status": "paused",
                                                   "paused_by": rec.rec_id}
            else:
                version = self._new_version(user_id, "system", "例行训练安排",
                                            None, sessions, current, now)
                rec.plan_version_id = version.version_id
                self.store.plan_status[user_id] = {"status": "active",
                                                   "paused_by": None}

            self.store.emit("system", "recommendation_created", {
                "rec_id": rec.rec_id, "user_id": user_id, "status": status,
                "holds": [f.rule_id for f in holds],
                "required_confirmations": required,
            }, now)
            return rec

    # ---------- 确认 ----------

    def confirm(self, rec_id, actor, role, note="", now=None) -> Recommendation:
        now = now or utcnow()
        with self.store.lock:
            rec = self.store.recommendations.get(rec_id)
            if rec is None:
                raise NotFoundError(f"建议不存在：{rec_id}")
            if rec.status != "held":
                raise ConflictError("该建议不在待确认状态")
            if role not in rec.required_confirmations:
                raise ValidationError(f"该建议不需要 {role} 角色确认")
            if any(c["role"] == role for c in rec.confirmed_by):
                raise ConflictError(f"{role} 角色已确认过")
            if role == "runner" and actor != rec.user_id:
                raise ForbiddenError("只能由本人确认")
            if role == "medical" and not self._has_grant(
                    rec.user_id, "medical", actor, now):
                raise ForbiddenError("需要本人授权的医疗顾问确认")

            rec.confirmed_by.append(
                {"actor": actor, "role": role, "note": note or "", "at": iso(now)})
            self.store.emit(actor, "recommendation_confirmation", {
                "rec_id": rec_id, "user_id": rec.user_id, "role": role,
                "note": note or "",
            }, now)

            satisfied = {c["role"] for c in rec.confirmed_by}
            if set(rec.required_confirmations) <= satisfied:
                self._finalize_confirmation(rec, now)
            return rec

    def _finalize_confirmation(self, rec, now):
        rec.status = "confirmed"
        user = self.store.users[rec.user_id]
        # 时区变化经本人确认后正式采纳，后续按新时区归因。
        if any(f.rule_id == "timezone-change" for f in rec.findings) \
                and user.last_seen_tz and user.last_seen_tz != user.home_tz:
            old_tz = user.home_tz
            user.home_tz = user.last_seen_tz
            user.last_seen_tz = None
            self.store.emit(rec.user_id, "timezone_updated", {
                "user_id": rec.user_id, "from": old_tz, "to": user.home_tz,
            }, now)
        current = self.current_version(rec.user_id, now)
        version = self._new_version(rec.user_id, "system", "确认后恢复训练安排",
                                    None, rec.suggested_sessions, current, now)
        rec.plan_version_id = version.version_id
        self.store.plan_status[rec.user_id] = {"status": "active", "paused_by": None}
        self.store.emit("system", "recommendation_confirmed", {
            "rec_id": rec.rec_id, "user_id": rec.user_id,
            "plan_version_id": version.version_id,
        }, now)

    # ---------- 教练覆盖 ----------

    def apply_override(self, user_id, actor, sessions, reason, valid_until, now=None):
        """教练覆盖：必须给出原因与期限，原版本完整保留。"""
        now = now or utcnow()
        with self.store.lock:
            self._user(user_id)
            if not self._has_grant(user_id, "coach", actor, now):
                raise ForbiddenError("只有本人授权的教练可以覆盖计划")
            if not reason or not str(reason).strip():
                raise ValidationError("覆盖必须填写原因")
            if valid_until is None:
                raise ValidationError("覆盖必须设定期限 valid_until")
            valid_until = parse_ts(valid_until)
            if valid_until <= now:
                raise ValidationError("覆盖期限必须晚于当前时间")
            if not sessions:
                raise ValidationError("覆盖后的训练安排不能为空")
            current = self.current_version(user_id, now)
            version = self._new_version(user_id, actor, str(reason).strip(),
                                        valid_until, sessions, current, now)
            self.store.emit(actor, "override_applied", {
                "user_id": user_id, "version_id": version.version_id,
                "reason": version.reason, "valid_until": iso(valid_until),
                "supersedes": version.supersedes,
            }, now)
            return version

    # ---------- 计划版本 ----------

    def current_version(self, user_id, now=None):
        """当前生效版本：过期覆盖自动回落到它取代的原版本。"""
        now = now or utcnow()
        for version in reversed(self.store.plan_versions.get(user_id, [])):
            if version.valid_until is None or version.valid_until > now:
                return version
        return None

    def _new_version(self, user_id, created_by, reason, valid_until, sessions,
                     current, now) -> PlanVersion:
        versions = self.store.plan_versions.setdefault(user_id, [])
        version = PlanVersion(
            version_id=self.store.new_id("ver"),
            user_id=user_id,
            version_no=len(versions) + 1,
            created_by=created_by,
            reason=reason,
            valid_until=valid_until,
            sessions=[dict(s) for s in sessions],
            supersedes=current.version_id if current else None,
            created_at=now,
        )
        versions.append(version)
        self.store.emit(created_by, "plan_version_created", {
            "user_id": user_id, "version_id": version.version_id,
            "reason": reason, "valid_until": iso(valid_until),
            "supersedes": version.supersedes,
        }, now)
        return version

    # ---------- 负荷与排课 ----------

    def week_load(self, user, now=None) -> float:
        """本周（用户时区）已确认的规范化负荷；去重与修正在此生效。"""
        now = now or utcnow()
        year, week, _ = now.astimezone(ZoneInfo(user.home_tz)).isocalendar()
        total = 0.0
        for record in self.ingest.active_records(user.user_id):
            r_year, r_week, _ = date.fromisoformat(record.local_date).isocalendar()
            if (r_year, r_week) == (year, week):
                total += record.load
        return round(total, 2)

    def _session_load(self, user) -> float:
        return BASE_SESSION_MIN * DEFAULT_SLOW_JOG_INTENSITY \
            * GROUP_LOAD_MULTIPLIER[user.group]

    def _base_sessions(self, user) -> list:
        return self._sessions_for(user, self._base_week_load(user))

    def _base_week_load(self, user) -> float:
        return round(3 * self._session_load(user), 2)

    def _sessions_for(self, user, target_load) -> list:
        count = min(7, max(1, math.ceil(target_load / self._session_load(user) - 1e-9)))
        return [
            {"weekday": SESSION_WEEKDAYS[i], "kind": "超慢跑",
             "duration_min": BASE_SESSION_MIN,
             "intensity": DEFAULT_SLOW_JOG_INTENSITY,
             "pace_kmh": "4-6"}
            for i in range(count)
        ]

    # ---------- 内部 ----------

    def _user(self, user_id):
        user = self.store.users.get(user_id)
        if user is None:
            raise NotFoundError(f"用户不存在：{user_id}")
        return user

    def _has_grant(self, user_id, role, grantee_id, now) -> bool:
        return any(
            g.user_id == user_id and g.role == role and g.grantee_id == grantee_id
            and (g.valid_until is None or g.valid_until > now)
            for g in self.store.grants
        )
