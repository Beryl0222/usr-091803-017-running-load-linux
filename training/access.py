"""角色访问控制：跑者看本人完整细节，教练/医疗凭授权各看摘要。

- runner：仅本人，完整视图；
- coach：需跑者授予的 training 授权，看训练摘要（负荷/计划/风险标记名），
  不看原始心率、睡眠、主观疲劳等健康细节；
- medical：需跑者授予的 medical 授权，看医疗摘要（风险判断、心率统计、伤病标签）；
- 回放同样按角色投影：结构完整，但专业人员只看到授权范围内的输入摘要。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .errors import ForbiddenError
from .models import Actor, Role
from .store import Store, utcnow

FULL = "full"
COACH_SUMMARY = "coach_summary"
MEDICAL_SUMMARY = "medical_summary"


def view_kind(store: Store, actor: Actor, runner_id: str, now: Optional[datetime] = None) -> str:
    """判定执行者对某跑者数据的可见级别；无权时抛 ForbiddenError。"""
    now = now or utcnow()
    if actor.role == Role.SYSTEM:
        return FULL
    if actor.role == Role.RUNNER:
        if actor.actor_id != runner_id:
            raise ForbiddenError("跑者只能查看自己的健康细节")
        return FULL
    if actor.role in (Role.COACH, Role.MEDICAL):
        profile = store.get_runner(runner_id)
        if profile.find_authorization(actor.role, actor.actor_id, now) is not None:
            return COACH_SUMMARY if actor.role == Role.COACH else MEDICAL_SUMMARY
        raise ForbiddenError("缺少跑者授予的有效授权")
    raise ForbiddenError("未知角色")


def project_recommendation(rec, kind: str, now: Optional[datetime] = None) -> dict:
    base = {
        "recommendation_id": rec.recommendation_id,
        "runner_id": rec.runner_id,
        "week": rec.week,
        "version": rec.version,
        "status": rec.status.value,
        "created_at": rec.created_at.isoformat(),
        "target_weekly_load": rec.target_weekly_load,
        "recent_load": rec.recent_load,
        "planned_sessions": [s.to_dict() for s in rec.planned_sessions],
        "risk_flags": [f.value for f in rec.risk_flags],
        "confirmations": [c.to_dict() for c in rec.confirmations],
        "overrides": [o.to_dict(now=now) for o in rec.overrides],
        "confirmation_request": rec.confirmation_request,
    }
    if kind == FULL:
        base["risk_details"] = rec.risk_details
        base["explanation"] = rec.explanation
        base["input_snapshot"] = rec.input_snapshot
        base["supersedes"] = rec.supersedes
    elif kind == COACH_SUMMARY:
        # 训练摘要：规则说明保留（不含原始健康数值），风险只给标记名。
        base["explanation_rules"] = [r["rule"] for r in rec.explanation.get("rules", [])]
    elif kind == MEDICAL_SUMMARY:
        # 医疗摘要：风险判断细节与规则说明，不含逐条原始记录。
        base["risk_details"] = rec.risk_details
        base["explanation_rules"] = rec.explanation.get("rules", [])
    return base


def project_runner(profile, kind: str) -> dict:
    base = {
        "runner_id": profile.runner_id,
        "groups": [g.value for g in profile.groups],
        "home_timezone": profile.home_timezone,
    }
    if kind == FULL:
        base.update({
            "resting_hr": profile.resting_hr,
            "max_hr": profile.max_hr,
            "baseline_weekly_load": profile.baseline_weekly_load,
            "injury_tags": [t.to_dict() for t in profile.injury_tags],
            "authorizations": [a.to_dict() for a in profile.authorizations],
            "last_device_timezone": profile.last_device_timezone,
        })
    elif kind == COACH_SUMMARY:
        base["active_injury"] = bool(profile.active_injuries())
        base["baseline_weekly_load"] = profile.baseline_weekly_load
    elif kind == MEDICAL_SUMMARY:
        base.update({
            "resting_hr": profile.resting_hr,
            "max_hr": profile.max_hr,
            "injury_tags": [t.to_dict() for t in profile.injury_tags],
        })
    return base


def project_replay(raw: dict, kind: str) -> dict:
    """回放投影：任何人都能看到判断链与人工调整，但原始记录按角色裁剪。"""
    if kind == FULL:
        return raw
    projected = {k: v for k, v in raw.items() if k != "input_snapshot"}
    snapshot = raw.get("input_snapshot", {})
    if kind == COACH_SUMMARY:
        projected["input_summary"] = {
            "session_count": len(snapshot.get("sessions", [])),
            "session_loads": snapshot.get("session_loads", {}),
            "readiness_score": snapshot.get("readiness_score"),
        }
    elif kind == MEDICAL_SUMMARY:
        projected["input_summary"] = {
            "sessions": [
                {"day": s["home_date"], "avg_hr": s["avg_hr"], "max_hr": s["max_hr"],
                 "duration_min": s["duration_min"]}
                for s in snapshot.get("sessions", [])
            ],
            "daily_metrics": [
                {"day": d["day"], "sleep_hours": d["sleep_hours"],
                 "fatigue_score": d["fatigue_score"],
                 "morning_resting_hr": d["morning_resting_hr"]}
                for d in snapshot.get("daily_metrics", [])
            ],
            "profile": snapshot.get("profile"),
            "readiness_score": snapshot.get("readiness_score"),
        }
    return projected
