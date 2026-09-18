"""跑者档案注册与授权管理。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .errors import ForbiddenError, ValidationError
from .ingestion import parse_datetime
from .models import (ROLE_SCOPE, Actor, Authorization, InjuryTag, PopulationGroup,
                     Role, RunnerProfile)
from .store import Store, utcnow


def _parse_groups(value) -> tuple:
    if value is None:
        return (PopulationGroup.GENERAL,)
    if not isinstance(value, list) or not value:
        raise ValidationError("groups 必须是非空数组")
    groups = []
    for item in value:
        try:
            groups.append(PopulationGroup(item))
        except ValueError:
            raise ValidationError(f"未知人群标签: {item}")
    return tuple(dict.fromkeys(groups))


def create_runner(store: Store, payload: dict, actor: Actor,
                  now: Optional[datetime] = None) -> RunnerProfile:
    """注册跑者档案。允许系统或本人（同名 runner 账号）创建。"""
    now = now or utcnow()
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    runner_id = payload.get("runner_id")
    if not isinstance(runner_id, str) or not runner_id.strip():
        raise ValidationError("缺少必填字段: runner_id")
    runner_id = runner_id.strip()
    if actor.role == Role.RUNNER and actor.actor_id != runner_id:
        raise ForbiddenError("跑者只能为自己创建档案")
    if actor.role not in (Role.RUNNER, Role.SYSTEM, Role.COACH):
        raise ForbiddenError("该角色不能创建跑者档案")
    if runner_id in store.runners:
        raise ValidationError(f"跑者已存在: {runner_id}")

    resting_hr = payload.get("resting_hr")
    max_hr = payload.get("max_hr")
    baseline = payload.get("baseline_weekly_load")
    for name, value in (("resting_hr", resting_hr), ("max_hr", max_hr)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{name} 必须是正整数")
    if max_hr <= resting_hr:
        raise ValidationError("max_hr 必须大于 resting_hr")
    if not isinstance(baseline, (int, float)) or isinstance(baseline, bool) or baseline < 0:
        raise ValidationError("baseline_weekly_load 必须是非负数字")

    home_timezone = payload.get("home_timezone", "Asia/Shanghai")
    try:
        ZoneInfo(home_timezone)
    except Exception:
        raise ValidationError(f"未知时区: {home_timezone}")

    injury_tags = []
    for item in payload.get("injury_tags") or []:
        if not isinstance(item, dict) or not item.get("label"):
            raise ValidationError("injury_tags 元素必须包含 label")
        injury_tags.append(InjuryTag(
            label=str(item["label"]),
            active=bool(item.get("active", True)),
            noted_by=str(item.get("noted_by", actor.actor_id)),
            noted_at=now,
        ))

    profile = RunnerProfile(
        runner_id=runner_id,
        resting_hr=resting_hr,
        max_hr=max_hr,
        baseline_weekly_load=float(baseline),
        groups=_parse_groups(payload.get("groups")),
        home_timezone=home_timezone,
        injury_tags=injury_tags,
        created_at=now,
    )
    store.add_runner(profile)
    store.log("runner_registered", actor.actor_id, actor.role, runner_id, runner_id,
              {"groups": [g.value for g in profile.groups]}, at=now)
    return profile


def grant_authorization(store: Store, runner_id: str, payload: dict, actor: Actor,
                        now: Optional[datetime] = None) -> Authorization:
    """跑者本人授予教练/医疗顾问限时访问授权。"""
    now = now or utcnow()
    if not (actor.role == Role.RUNNER and actor.actor_id == runner_id):
        raise ForbiddenError("只有跑者本人可以授予授权")
    profile = store.get_runner(runner_id)
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    try:
        grantee_role = Role(payload.get("grantee_role"))
    except ValueError:
        raise ValidationError("grantee_role 必须是 coach 或 medical")
    if grantee_role not in (Role.COACH, Role.MEDICAL):
        raise ValidationError("grantee_role 必须是 coach 或 medical")
    grantee_id = payload.get("grantee_id")
    if not isinstance(grantee_id, str) or not grantee_id.strip():
        raise ValidationError("缺少必填字段: grantee_id")
    expires_at = parse_datetime(payload.get("expires_at"), "expires_at")
    if expires_at <= now:
        raise ValidationError("expires_at 必须是将来的时间")

    scope = ROLE_SCOPE[grantee_role]
    auth = Authorization(grantee_role=grantee_role, grantee_id=grantee_id.strip(),
                         scope=scope, granted_at=now, expires_at=expires_at)
    profile.authorizations.append(auth)
    store.log("authorization_granted", actor.actor_id, actor.role, runner_id, runner_id,
              {"grantee_role": grantee_role.value, "grantee_id": grantee_id.strip(),
               "scope": scope, "expires_at": expires_at.isoformat()}, at=now)
    return auth
