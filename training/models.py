"""领域对象：跑者档案、设备记录、训练建议、覆盖与审计事件。

设计要点：
- 所有记录只增不改：手工修正产生新版本并指回原版本，建议被取代时保留原版本；
- 每条建议冻结当时的输入快照与规则说明，支撑事后回放；
- 时间一律使用带时区的 datetime，归属日期按跑者常驻时区计算。
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


class PopulationGroup(str, enum.Enum):
    """人群标签：同样的 30 分钟超慢跑对不同人群不是同一种负荷。"""

    GENERAL = "general"    # 普通跑者
    BEGINNER = "beginner"  # 初学者
    CHRONIC = "chronic"    # 慢病人群
    RECOVERY = "recovery"  # 伤后恢复者


class RiskFlag(str, enum.Enum):
    """风险信号：任一出现即不自动加量，暂停相关安排并请求确认。"""

    ABNORMAL_HEART_RATE = "abnormal_heart_rate"  # 异常心率
    ACTIVE_INJURY = "active_injury"              # 伤病标签
    TIMEZONE_CHANGE = "timezone_change"          # 时区变化
    MISSING_KEY_DATA = "missing_key_data"        # 关键数据缺失


class RecommendationStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"          # 已暂停，等待本人或专业人员确认
    SUPERSEDED = "superseded"  # 被更新的建议版本取代


class Role(str, enum.Enum):
    RUNNER = "runner"
    COACH = "coach"
    MEDICAL = "medical"
    SYSTEM = "system"


# 授权范围：教练看训练摘要，医疗顾问看医疗摘要。
ROLE_SCOPE = {Role.COACH: "training", Role.MEDICAL: "medical"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


@dataclass
class Actor:
    """一次操作的执行者（来自请求头或系统内部）。"""

    actor_id: str
    role: Role


@dataclass
class InjuryTag:
    label: str
    active: bool = True
    noted_by: str = ""
    noted_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "active": self.active,
            "noted_by": self.noted_by,
            "noted_at": iso(self.noted_at),
        }


@dataclass
class Authorization:
    """跑者授予专业人员的访问授权，带期限。"""

    grantee_role: Role
    grantee_id: str
    scope: str
    granted_at: datetime
    expires_at: datetime

    def is_valid(self, now: datetime) -> bool:
        return self.expires_at > now

    def to_dict(self) -> dict:
        return {
            "grantee_role": self.grantee_role.value,
            "grantee_id": self.grantee_id,
            "scope": self.scope,
            "granted_at": iso(self.granted_at),
            "expires_at": iso(self.expires_at),
        }


@dataclass
class RunnerProfile:
    runner_id: str
    resting_hr: int
    max_hr: int
    baseline_weekly_load: float
    groups: tuple = (PopulationGroup.GENERAL,)
    home_timezone: str = "Asia/Shanghai"
    injury_tags: list = field(default_factory=list)
    authorizations: list = field(default_factory=list)
    created_at: Optional[datetime] = None
    # 最近一次设备同步上报的时区，用于识别时区变化。
    last_device_timezone: Optional[str] = None

    def active_injuries(self) -> list:
        return [t for t in self.injury_tags if t.active]

    def find_authorization(self, role: Role, grantee_id: str, now: datetime) -> Optional[Authorization]:
        for auth in self.authorizations:
            if auth.grantee_role == role and auth.grantee_id == grantee_id and auth.is_valid(now):
                return auth
        return None

    def baseline_dict(self) -> dict:
        return {
            "resting_hr": self.resting_hr,
            "max_hr": self.max_hr,
            "baseline_weekly_load": self.baseline_weekly_load,
            "groups": [g.value for g in self.groups],
            "home_timezone": self.home_timezone,
        }


@dataclass
class SessionRecord:
    """一次跑步记录。重复同步按 (source, external_id) 去重，只计一次负荷。"""

    record_id: str
    runner_id: str
    source: str
    external_id: str
    started_at: datetime
    duration_min: float
    home_date: date  # started_at 换算到跑者常驻时区后的归属日期
    device_timezone: str
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    cadence_spm: Optional[int] = None
    distance_km: Optional[float] = None
    supersedes: Optional[str] = None     # 本记录修正了哪条原始 external_id
    superseded_by: Optional[str] = None  # 本记录被哪条 record_id 取代
    ingested_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "runner_id": self.runner_id,
            "source": self.source,
            "external_id": self.external_id,
            "started_at": iso(self.started_at),
            "duration_min": self.duration_min,
            "home_date": self.home_date.isoformat(),
            "device_timezone": self.device_timezone,
            "avg_hr": self.avg_hr,
            "max_hr": self.max_hr,
            "cadence_spm": self.cadence_spm,
            "distance_km": self.distance_km,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "ingested_at": iso(self.ingested_at),
        }


@dataclass
class DailyMetrics:
    """每日恢复类指标：睡眠、主观疲劳、晨起静息心率。"""

    metrics_id: str
    runner_id: str
    source: str
    external_id: str
    day: date
    sleep_hours: Optional[float] = None
    fatigue_score: Optional[int] = None  # 1-10 主观疲劳
    morning_resting_hr: Optional[int] = None
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    ingested_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "metrics_id": self.metrics_id,
            "runner_id": self.runner_id,
            "source": self.source,
            "external_id": self.external_id,
            "day": self.day.isoformat(),
            "sleep_hours": self.sleep_hours,
            "fatigue_score": self.fatigue_score,
            "morning_resting_hr": self.morning_resting_hr,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "ingested_at": iso(self.ingested_at),
        }


@dataclass
class PlannedSession:
    day: date
    duration_min: int
    target_hr_low: int
    target_hr_high: int
    purpose: str
    suspended: bool = False  # 风险暂停期间不生效

    def to_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "duration_min": self.duration_min,
            "target_hr_low": self.target_hr_low,
            "target_hr_high": self.target_hr_high,
            "purpose": self.purpose,
            "suspended": self.suspended,
        }


@dataclass
class Confirmation:
    actor_id: str
    actor_role: Role
    at: datetime
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "actor_id": self.actor_id,
            "actor_role": self.actor_role.value,
            "at": iso(self.at),
            "note": self.note,
        }


@dataclass
class Override:
    """教练/医疗覆盖：必须保留原因、期限和原版本。"""

    override_id: str
    actor_id: str
    actor_role: Role
    reason: str
    expires_at: datetime
    changes: dict
    original: dict  # 被覆盖前的字段快照（原版本）
    created_at: datetime
    version_before: int

    def is_active(self, now: datetime) -> bool:
        return self.expires_at > now

    def to_dict(self, now: Optional[datetime] = None) -> dict:
        data = {
            "override_id": self.override_id,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role.value,
            "reason": self.reason,
            "expires_at": iso(self.expires_at),
            "changes": self.changes,
            "original": self.original,
            "created_at": iso(self.created_at),
            "version_before": self.version_before,
        }
        if now is not None:
            data["active"] = self.is_active(now)
        return data


@dataclass
class Recommendation:
    """一次训练建议。冻结输入快照与规则说明，支持事后回放。"""

    recommendation_id: str
    runner_id: str
    week: str  # 目标 ISO 周，如 2026-W39
    created_at: datetime
    status: RecommendationStatus
    target_weekly_load: float
    recent_load: float  # 生成时最近 7 天已完成负荷
    planned_sessions: list
    risk_flags: list
    risk_details: dict
    explanation: dict      # 采用了哪些数据与规则
    input_snapshot: dict   # 生成时的原始记录快照（回放依据）
    version: int = 1
    supersedes: Optional[str] = None
    confirmations: list = field(default_factory=list)
    overrides: list = field(default_factory=list)
    confirmation_request: Optional[dict] = None  # 暂停时向本人/专业人员发起的确认请求


@dataclass
class Event:
    """追加式审计事件：谁、何时、对什么、做了什么、为什么。"""

    seq: int
    type: str
    actor_id: str
    actor_role: Role
    at: datetime
    entity_id: str
    runner_id: str
    details: dict

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "type": self.type,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role.value,
            "at": iso(self.at),
            "entity_id": self.entity_id,
            "runner_id": self.runner_id,
            "details": self.details,
        }
