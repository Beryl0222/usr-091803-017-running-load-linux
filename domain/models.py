"""领域对象：用户基线、指标批次、训练记录、计划版本与建议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# 人群负荷系数：同样的 30 分钟超慢跑，对不同人群并不是同一种负荷。
GROUP_LOAD_MULTIPLIER = {
    "regular": 1.0,
    "beginner": 1.2,
    "chronic": 1.5,
    "recovering": 1.8,
}

GROUP_LABELS = {
    "regular": "常规跑者",
    "beginner": "初学者",
    "chronic": "慢病人群",
    "recovering": "伤后恢复者",
}

# 超慢跑（每小时 4–6 千米）缺少心率数据时采用的默认强度（占最大心率比例）。
DEFAULT_SLOW_JOG_INTENSITY = 0.6

# 单次训练平均心率超过最大心率该比例即视为异常心率。
ABNORMAL_HR_RATIO = 0.85

# 静息心率高出个人基线该比例即视为异常。
RESTING_HR_ELEVATION = 1.1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value) -> datetime:
    """把 ISO 字符串或 datetime 统一成带时区的 UTC 时间。"""
    if isinstance(value, datetime):
        ts = value
    else:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def iso(ts: datetime | None) -> str | None:
    return ts.astimezone(timezone.utc).isoformat() if ts else None


@dataclass
class User:
    """跑者及其个人基线；训练建议以此为准。"""

    user_id: str
    group: str
    resting_hr: int
    max_hr: int
    home_tz: str
    weekly_capacity: float
    created_at: datetime
    last_seen_tz: str | None = None  # 设备最近上报的时区，与 home_tz 不一致时触发时区规则


@dataclass
class Sample:
    """单条指标样本：workout / heart_rate / sleep / fatigue。"""

    kind: str
    recorded_at: datetime
    value: dict


@dataclass
class MetricBatch:
    """一次设备同步批次，sync_id 是幂等键。"""

    sync_id: str
    user_id: str
    device_id: str
    tz: str
    source: str  # device | manual
    corrects: str | None  # 手工修正指向的原始 sync_id
    synced_at: datetime
    samples: list = field(default_factory=list)
    status: str = "accepted"  # accepted | superseded


@dataclass
class WorkoutRecord:
    """从批次中提炼的规范化训练记录，负荷只在这里计算一次。"""

    record_id: str
    user_id: str
    sync_id: str
    recorded_at: datetime
    local_date: str  # 训练发生地在记录时区的日期，跨日同步按此归因
    duration_min: float
    avg_hr: float | None
    avg_cadence: float | None
    distance_km: float | None
    load: float
    source: str
    status: str = "active"  # active | superseded
    corrected_by: str | None = None


@dataclass
class InjuryTag:
    user_id: str
    label: str
    note: str
    created_by: str
    created_at: datetime
    cleared_at: datetime | None = None


@dataclass
class Finding:
    """一条规则判定结果；severity 为 hold 时建议进入待确认。"""

    rule_id: str
    severity: str  # hold | info
    summary: str
    detail: dict
    confirmer: str | None  # runner | medical


@dataclass
class Recommendation:
    rec_id: str
    user_id: str
    created_at: datetime
    status: str  # ready | held | confirmed
    findings: list
    required_confirmations: list
    explanation: dict  # 采用了哪些数据与规则
    suggested_sessions: list
    suggestion_basis: str
    confirmed_by: list = field(default_factory=list)
    plan_version_id: str | None = None


@dataclass
class PlanVersion:
    """计划的一个版本；调整与覆盖都保留原因、期限和原版本。"""

    version_id: str
    user_id: str
    version_no: int
    created_by: str  # system 或教练 id
    reason: str
    valid_until: datetime | None  # 期限；None 表示直至被下一版本替代
    sessions: list
    supersedes: str | None
    created_at: datetime


@dataclass
class Grant:
    """本人签发的授权：教练或医疗顾问据此查看对应摘要。"""

    user_id: str
    role: str  # coach | medical
    grantee_id: str
    created_at: datetime
    valid_until: datetime | None = None


@dataclass
class Event:
    """追加式事件，所有状态变化的责任追溯依据。"""

    event_id: str
    ts: datetime
    actor: str
    type: str
    payload: dict
