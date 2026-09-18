"""训练建议：生成、暂停、确认、覆盖与回放。

核心安全不变量：
- 存在风险信号（异常心率/伤病标签/时区变化/关键数据缺失）时，
  目标负荷只降不增，相关安排暂停，等待本人或专业人员确认；
- 每条建议冻结输入快照与规则说明，回放时完整呈现原始记录、
  风险判断以及后来是否被人工调整；
- 计划调整（新版本取代旧版本）与教练覆盖都保留原因、期限和原版本。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .errors import ConflictError, ForbiddenError, ValidationError
from .load import increase_cap_for, readiness, sensitivity_for, window_load
from .models import (Actor, Confirmation, Override, PlannedSession,
                     Recommendation, RecommendationStatus, Role, new_id)
from .risk import assess
from .store import Store, utcnow

# 计划编排常数
ASSUMED_INTENSITY = 0.5       # 超慢跑目标强度（心率储备占比）
SESSIONS_PER_WEEK = 3
MIN_SESSION_MIN = 20
MAX_SESSION_MIN = 45
READINESS_TAPER_THRESHOLD = 60
READINESS_TAPER_FACTOR = 0.8
NEW_RUNNER_BASELINE_FACTOR = 0.5  # 无历史负荷时按个人基线的 50% 起步

ALLOWED_OVERRIDE_KEYS = {"target_weekly_load", "planned_sessions"}


def generate(store: Store, runner_id: str, actor: Actor,
             now: Optional[datetime] = None) -> Recommendation:
    """为跑者生成下一 ISO 周的训练建议。"""
    now = now or utcnow()
    with store.lock:
        profile = store.get_runner(runner_id)
        today = now.astimezone(ZoneInfo(profile.home_timezone)).date()

        window = window_load(store, profile, today)
        recent_dailies = [d for d in store.dailies_for(runner_id)
                          if today - timedelta(days=2) <= d.day <= today]
        readiness_score, readiness_parts = readiness(profile, recent_dailies)
        risk = assess(store, profile, today)

        sensitivity = sensitivity_for(profile.groups)
        cap = increase_cap_for(profile.groups)
        rules = [{
            "rule": "personalized_load_sensitivity",
            "detail": (f"人群标签 {[g.value for g in profile.groups]} → 敏感系数 {sensitivity}，"
                       "同样的 30 分钟超慢跑按更高负荷计入"),
            "coefficient": sensitivity,
        }]

        # 目标负荷：有历史则按增幅上限，无历史按个人基线的一半起步。
        if window.total > 0:
            target = window.total * (1 + cap)
            rules.append({
                "rule": "weekly_increase_cap",
                "detail": (f"周负荷增幅上限 {cap:.0%}：最近 7 天负荷 "
                           f"{window.total:.1f} → 目标上限 {target:.1f}"),
                "cap": cap,
            })
        else:
            target = profile.baseline_weekly_load * NEW_RUNNER_BASELINE_FACTOR
            rules.append({
                "rule": "baseline_start",
                "detail": (f"最近 7 天无有效负荷，按个人基线 "
                           f"{profile.baseline_weekly_load:.1f} 的 50% 起步 → {target:.1f}"),
            })

        if readiness_score < READINESS_TAPER_THRESHOLD:
            target *= READINESS_TAPER_FACTOR
            rules.append({
                "rule": "readiness_taper",
                "detail": (f"恢复状态 {readiness_score} 低于 {READINESS_TAPER_THRESHOLD}，"
                           f"目标下调至 {target:.1f}（只降不增）"),
                "readiness": readiness_score,
            })

        # 风险闸门：任何风险信号 → 不自动加量并暂停安排。
        status = RecommendationStatus.ACTIVE
        confirmation_request = None
        if not risk.ok:
            if target > window.total:
                target = window.total
            status = RecommendationStatus.PAUSED
            confirmation_request = {
                "addressed_to": [Role.RUNNER.value, Role.COACH.value, Role.MEDICAL.value],
                "reason": [f.value for f in risk.flags],
                "requested_at": now.isoformat(),
            }
            rules.append({
                "rule": "risk_no_increase",
                "detail": ("存在风险信号，目标不超过最近 7 天负荷并暂停相关安排，"
                           "等待本人或专业人员确认"),
                "flags": [f.value for f in risk.flags],
            })

        planned = _plan_sessions(profile, target, sensitivity, today,
                                 suspended=status is RecommendationStatus.PAUSED)

        # 取代上一版建议（计划调整保留原版本）。
        previous = None
        for rec in store.recommendations_for(runner_id):
            if rec.status in (RecommendationStatus.ACTIVE, RecommendationStatus.PAUSED):
                previous = rec
        if previous is not None:
            previous.status = RecommendationStatus.SUPERSEDED

        data_used = ["personal_baseline", "injury_tags", "timezone"]
        if window.considered:
            data_used.append("heart_rate")
        if any(s.cadence_spm is not None for s, _ in window.considered):
            data_used.append("cadence")
        if any(d.sleep_hours is not None for d in recent_dailies):
            data_used.append("sleep")
        if any(d.fatigue_score is not None for d in recent_dailies):
            data_used.append("subjective_fatigue")

        rec = Recommendation(
            recommendation_id=new_id("rec"),
            runner_id=runner_id,
            week=_next_week_label(today),
            created_at=now,
            status=status,
            target_weekly_load=round(target, 2),
            recent_load=round(window.total, 2),
            planned_sessions=planned,
            risk_flags=list(risk.flags),
            risk_details=risk.details,
            explanation={
                "data_used": data_used,
                "rules": rules,
                "readiness": {"score": readiness_score, "parts": readiness_parts},
            },
            input_snapshot=_snapshot(profile, window, recent_dailies, readiness_score),
            supersedes=previous.recommendation_id if previous else None,
            confirmation_request=confirmation_request,
        )
        store.add_recommendation(rec)
        store.log("recommendation_created", actor.actor_id, actor.role,
                  rec.recommendation_id, runner_id,
                  {"week": rec.week, "target_weekly_load": rec.target_weekly_load,
                   "status": status.value,
                   "supersedes": rec.supersedes}, at=now)
        if status is RecommendationStatus.PAUSED:
            store.log("recommendation_paused", actor.actor_id, actor.role,
                      rec.recommendation_id, runner_id,
                      {"flags": [f.value for f in risk.flags]}, at=now)
        return rec


def confirm(store: Store, recommendation_id: str, actor: Actor, note: str = "",
            now: Optional[datetime] = None) -> Recommendation:
    """本人或授权专业人员确认后，暂停的建议恢复生效。"""
    now = now or utcnow()
    with store.lock:
        rec = store.get_recommendation(recommendation_id)
        if rec.status is not RecommendationStatus.PAUSED:
            raise ConflictError("只有处于暂停状态的建议可以确认")
        _require_self_or_professional(store, rec, actor, now)

        rec.confirmations.append(Confirmation(actor.actor_id, actor.role, now, note or ""))
        rec.status = RecommendationStatus.ACTIVE
        for session in rec.planned_sessions:
            session.suspended = False
        rec.confirmation_request = None
        store.log("recommendation_confirmed", actor.actor_id, actor.role,
                  rec.recommendation_id, rec.runner_id, {"note": note or ""}, at=now)
        return rec


def override(store: Store, recommendation_id: str, actor: Actor, reason: str,
             expires_at: datetime, changes: dict,
             now: Optional[datetime] = None) -> Recommendation:
    """教练/医疗覆盖：必须给出原因与期限，原版本完整保留。"""
    now = now or utcnow()
    with store.lock:
        rec = store.get_recommendation(recommendation_id)
        profile = store.get_runner(rec.runner_id)
        if actor.role not in (Role.COACH, Role.MEDICAL):
            raise ForbiddenError("只有教练或医疗顾问可以覆盖建议")
        if profile.find_authorization(actor.role, actor.actor_id, now) is None:
            raise ForbiddenError("缺少跑者授予的有效授权")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("覆盖必须填写原因 reason")
        if expires_at is None or expires_at.tzinfo is None:
            raise ValidationError("覆盖必须提供携带时区的期限 expires_at")
        if expires_at <= now:
            raise ValidationError("expires_at 必须是将来的时间")
        if not isinstance(changes, dict) or not changes:
            raise ValidationError("覆盖必须包含 changes")
        unknown = set(changes) - ALLOWED_OVERRIDE_KEYS
        if unknown:
            raise ValidationError(f"不支持的覆盖字段: {sorted(unknown)}")

        original = {
            "target_weekly_load": rec.target_weekly_load,
            "planned_sessions": [s.to_dict() for s in rec.planned_sessions],
            "status": rec.status.value,
        }
        if "target_weekly_load" in changes:
            value = changes["target_weekly_load"]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ValidationError("target_weekly_load 必须是非负数字")
            rec.target_weekly_load = round(float(value), 2)
        if "planned_sessions" in changes:
            rec.planned_sessions = _parse_planned_sessions(changes["planned_sessions"])

        rec.overrides.append(Override(
            override_id=new_id("ovr"),
            actor_id=actor.actor_id,
            actor_role=actor.role,
            reason=reason.strip(),
            expires_at=expires_at,
            changes=changes,
            original=original,
            created_at=now,
            version_before=rec.version,
        ))
        rec.version += 1
        store.log("override_applied", actor.actor_id, actor.role,
                  rec.recommendation_id, rec.runner_id,
                  {"reason": reason.strip(), "expires_at": expires_at.isoformat(),
                   "changes": changes, "version_before": rec.version - 1}, at=now)
        return rec


def replay(store: Store, recommendation_id: str, now: Optional[datetime] = None) -> dict:
    """完整回放：原始记录、风险判断、以及后来是否被人工调整。"""
    now = now or utcnow()
    rec = store.get_recommendation(recommendation_id)
    adjustments = []
    for c in rec.confirmations:
        adjustments.append({"type": "confirmation", **c.to_dict()})
    for o in rec.overrides:
        adjustments.append({"type": "override", **o.to_dict(now=now)})
    return {
        "recommendation_id": rec.recommendation_id,
        "runner_id": rec.runner_id,
        "week": rec.week,
        "version": rec.version,
        "status": rec.status.value,
        "generated_at": rec.created_at.isoformat(),
        "supersedes": rec.supersedes,
        "input_snapshot": rec.input_snapshot,
        "risk_assessment": {"flags": [f.value for f in rec.risk_flags],
                            "details": rec.risk_details},
        "explanation": rec.explanation,
        "target_weekly_load": rec.target_weekly_load,
        "planned_sessions": [s.to_dict() for s in rec.planned_sessions],
        "adjusted_by_human": bool(adjustments),
        "human_adjustments": adjustments,
        "events": [e.to_dict() for e in store.events_for_entity(rec.recommendation_id)],
    }


# ---- 内部 helpers ----

def _require_self_or_professional(store: Store, rec: Recommendation, actor: Actor,
                                  now: datetime) -> None:
    if actor.role == Role.RUNNER and actor.actor_id == rec.runner_id:
        return
    if actor.role in (Role.COACH, Role.MEDICAL):
        profile = store.get_runner(rec.runner_id)
        if profile.find_authorization(actor.role, actor.actor_id, now) is not None:
            return
    raise ForbiddenError("只有本人或获得授权的专业人员可以确认")


def _next_week_label(today) -> str:
    days_ahead = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_ahead)
    iso_year, iso_week, _ = monday.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _plan_sessions(profile, target: float, sensitivity: float, today,
                   suspended: bool) -> list:
    days_ahead = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_ahead)
    offsets = [0, 2, 5][:SESSIONS_PER_WEEK]  # 周一 / 周三 / 周六
    reserve = max(profile.max_hr - profile.resting_hr, 1)
    hr_low = round(profile.resting_hr + 0.5 * reserve)
    hr_high = round(profile.resting_hr + 0.65 * reserve)
    per_session = target / SESSIONS_PER_WEEK if target > 0 else 0
    raw_duration = per_session / (ASSUMED_INTENSITY * sensitivity) if per_session else MIN_SESSION_MIN
    duration = int(min(max(round(raw_duration / 5) * 5, MIN_SESSION_MIN), MAX_SESSION_MIN))
    return [
        PlannedSession(day=monday + timedelta(days=off), duration_min=duration,
                       target_hr_low=hr_low, target_hr_high=hr_high,
                       purpose="超慢跑（4-6 km/h 轻松有氧）", suspended=suspended)
        for off in offsets
    ]


def _parse_planned_sessions(items) -> list:
    if not isinstance(items, list) or not items:
        raise ValidationError("planned_sessions 必须是非空数组")
    from .ingestion import parse_date
    sessions = []
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("planned_sessions 元素必须是对象")
        day = parse_date(item.get("day"), "day")
        duration = item.get("duration_min")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            raise ValidationError("duration_min 必须是正整数")
        sessions.append(PlannedSession(
            day=day, duration_min=duration,
            target_hr_low=int(item.get("target_hr_low", 0)),
            target_hr_high=int(item.get("target_hr_high", 0)),
            purpose=str(item.get("purpose", "教练调整")),
            suspended=bool(item.get("suspended", False)),
        ))
    return sessions


def _snapshot(profile, window, recent_dailies, readiness_score) -> dict:
    """冻结生成建议时的全部输入，供事后回放。"""
    return {
        "profile": profile.baseline_dict(),
        "sessions": [record.to_dict() for record, _ in window.considered],
        "sessions_missing_hr": [r.external_id for r in window.missing_hr],
        "session_loads": {record.external_id: round(load, 2)
                          for record, load in window.considered},
        "daily_metrics": [d.to_dict() for d in recent_dailies],
        "readiness_score": readiness_score,
    }
