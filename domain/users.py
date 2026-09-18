"""用户目录：注册跑者、伤病标签与本人授权。"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from .models import (
    DEFAULT_SLOW_JOG_INTENSITY,
    GROUP_LABELS,
    GROUP_LOAD_MULTIPLIER,
    Grant,
    InjuryTag,
    User,
    parse_ts,
    utcnow,
)

GRANT_ROLES = ("coach", "medical")


def _check_tz(tz: str) -> None:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, KeyError, ValueError, TypeError):
        raise ValidationError(f"无法识别的时区：{tz}")


class DirectoryService:
    """维护用户、伤病标签与授权；所有变化都会写入事件日志。"""

    def __init__(self, store):
        self.store = store

    def create_user(self, user_id, group, resting_hr, max_hr, home_tz,
                    weekly_capacity=None, now=None):
        now = now or utcnow()
        with self.store.lock:
            if not user_id:
                raise ValidationError("缺少用户标识 user_id")
            if user_id in self.store.users:
                raise ConflictError(f"用户已存在：{user_id}")
            if group not in GROUP_LOAD_MULTIPLIER:
                raise ValidationError(
                    f"未知人群分组：{group}（可选：{', '.join(sorted(GROUP_LABELS))}）")
            _check_tz(home_tz)
            resting_hr, max_hr = int(resting_hr), int(max_hr)
            if not 30 <= resting_hr < max_hr <= 220:
                raise ValidationError("心率基线不合理：需要 30 <= 静息心率 < 最大心率 <= 220")
            if weekly_capacity is None:
                # 默认每周上限：5 次 30 分钟超慢跑折算的个人负荷。
                weekly_capacity = round(5 * 30 * DEFAULT_SLOW_JOG_INTENSITY
                                        * GROUP_LOAD_MULTIPLIER[group], 2)
            user = User(user_id=user_id, group=group, resting_hr=resting_hr,
                        max_hr=max_hr, home_tz=home_tz,
                        weekly_capacity=float(weekly_capacity), created_at=now)
            self.store.users[user_id] = user
            self.store.plan_status[user_id] = {"status": "active", "paused_by": None}
            self.store.emit(user_id, "user_created", {
                "user_id": user_id, "group": group,
                "group_label": GROUP_LABELS[group], "home_tz": home_tz,
            }, now)
            return user

    def get_user(self, user_id) -> User:
        user = self.store.users.get(user_id)
        if user is None:
            raise NotFoundError(f"用户不存在：{user_id}")
        return user

    def add_injury(self, user_id, label, actor, note="", now=None):
        now = now or utcnow()
        with self.store.lock:
            self.get_user(user_id)
            if not label or not str(label).strip():
                raise ValidationError("伤病标签不能为空")
            label = str(label).strip()
            for tag in self.store.injuries:
                if tag.user_id == user_id and tag.label == label and tag.cleared_at is None:
                    raise ConflictError(f"伤病标签已存在：{label}")
            tag = InjuryTag(user_id=user_id, label=label, note=note or "",
                            created_by=actor or "unknown", created_at=now)
            self.store.injuries.append(tag)
            self.store.emit(actor or "unknown", "injury_tag_added", {
                "user_id": user_id, "label": label, "note": note or "",
            }, now)
            return tag

    def clear_injury(self, user_id, label, actor, now=None):
        now = now or utcnow()
        with self.store.lock:
            self.get_user(user_id)
            for tag in self.store.injuries:
                if tag.user_id == user_id and tag.label == label and tag.cleared_at is None:
                    tag.cleared_at = now
                    self.store.emit(actor or "unknown", "injury_tag_cleared", {
                        "user_id": user_id, "label": label,
                    }, now)
                    return tag
            raise NotFoundError(f"没有进行中的伤病标签：{label}")

    def active_injuries(self, user_id):
        return [t for t in self.store.injuries
                if t.user_id == user_id and t.cleared_at is None]

    def grant(self, user_id, actor, role, grantee_id, valid_until=None, now=None):
        """本人签发授权；只有跑者本人能把摘要开放给教练或医疗顾问。"""
        now = now or utcnow()
        with self.store.lock:
            self.get_user(user_id)
            if actor != user_id:
                raise ForbiddenError("只能由本人授权他人查看摘要")
            if role not in GRANT_ROLES:
                raise ValidationError(f"未知授权角色：{role}（可选：coach / medical）")
            if not grantee_id:
                raise ValidationError("缺少被授权人 grantee_id")
            valid_until = parse_ts(valid_until) if valid_until else None
            if valid_until is not None and valid_until <= now:
                raise ValidationError("授权期限必须晚于当前时间")
            for g in self.store.grants:
                if (g.user_id == user_id and g.role == role
                        and g.grantee_id == grantee_id):
                    raise ConflictError("该授权已存在")
            grant = Grant(user_id=user_id, role=role, grantee_id=grantee_id,
                          created_at=now, valid_until=valid_until)
            self.store.grants.append(grant)
            self.store.emit(user_id, "grant_created", {
                "user_id": user_id, "role": role, "grantee_id": grantee_id,
            }, now)
            return grant

    def has_grant(self, user_id, role, grantee_id, now=None) -> bool:
        now = now or utcnow()
        return any(
            g.user_id == user_id and g.role == role and g.grantee_id == grantee_id
            and (g.valid_until is None or g.valid_until > now)
            for g in self.store.grants
        )
