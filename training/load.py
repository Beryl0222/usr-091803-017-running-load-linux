"""个性化训练负荷与恢复状态计算。

负荷模型（简化 TRIMP 思路）：
- 强度 = (平均心率 - 静息心率) / (最大心率 - 静息心率)，即心率储备占比，截断到 [0, 1.5]；
- 原始负荷 = 时长(分钟) × 强度；
- 个人负荷 = 原始负荷 × 人群敏感系数 —— 同样的 30 分钟超慢跑，
  对初学者、慢病人群和伤后恢复者计为更高的负荷；
- 周目标增幅上限同样按人群收紧。

恢复状态 readiness（0-100）由睡眠、主观疲劳、晨起静息心率合成，
只用于向下调节目标，绝不用于向上加量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from .models import PopulationGroup, RunnerProfile, SessionRecord

# 人群敏感系数：同样一节课，耐受越低的人群计负荷越高。
GROUP_SENSITIVITY = {
    PopulationGroup.GENERAL: 1.0,
    PopulationGroup.BEGINNER: 1.2,
    PopulationGroup.CHRONIC: 1.4,
    PopulationGroup.RECOVERY: 1.6,
}

# 周负荷增幅上限：耐受越低的人群加量越保守。
GROUP_MAX_WEEKLY_INCREASE = {
    PopulationGroup.GENERAL: 0.10,
    PopulationGroup.BEGINNER: 0.08,
    PopulationGroup.CHRONIC: 0.05,
    PopulationGroup.RECOVERY: 0.05,
}

INTENSITY_CAP = 1.5
RECENT_WINDOW_DAYS = 7


def sensitivity_for(groups) -> float:
    """多标签取最保守（最高）敏感系数。"""
    return max(GROUP_SENSITIVITY[g] for g in groups) if groups else 1.0


def increase_cap_for(groups) -> float:
    """多标签取最保守（最低）增幅上限。"""
    return min(GROUP_MAX_WEEKLY_INCREASE[g] for g in groups) if groups else 0.10


def session_intensity(profile: RunnerProfile, record: SessionRecord) -> Optional[float]:
    """心率储备强度；缺少心率数据时返回 None（关键数据缺失）。"""
    if record.avg_hr is None:
        return None
    reserve = profile.max_hr - profile.resting_hr
    if reserve <= 0:
        return None
    raw = (record.avg_hr - profile.resting_hr) / reserve
    return min(max(raw, 0.0), INTENSITY_CAP)


def session_load(profile: RunnerProfile, record: SessionRecord) -> Optional[float]:
    intensity = session_intensity(profile, record)
    if intensity is None:
        return None
    return record.duration_min * intensity * sensitivity_for(profile.groups)


@dataclass
class LoadWindow:
    """一个时间窗内的负荷汇总。"""

    total: float = 0.0
    considered: list = field(default_factory=list)  # (SessionRecord, load)
    missing_hr: list = field(default_factory=list)  # 缺心率、无法计负荷的记录

    @property
    def session_count(self) -> int:
        return len(self.considered) + len(self.missing_hr)


def window_load(store, profile: RunnerProfile, end_day: date,
                days: int = RECENT_WINDOW_DAYS) -> LoadWindow:
    """统计 [end_day - days + 1, end_day] 内（按归属日期）的有效负荷。

    只统计未被取代的记录；重复同步在摄入阶段已被去重，不会重复计入。
    """
    start_day = end_day - timedelta(days=days - 1)
    window = LoadWindow()
    for record in store.sessions_for(profile.runner_id):
        if not (start_day <= record.home_date <= end_day):
            continue
        load = session_load(profile, record)
        if load is None:
            window.missing_hr.append(record)
        else:
            window.total += load
            window.considered.append((record, load))
    return window


def readiness(profile: RunnerProfile, dailies: list) -> tuple:
    """根据最近每日指标计算恢复状态，返回 (分数, 构成明细)。"""
    score = 100
    parts = []
    if not dailies:
        return score, parts
    latest = dailies[-1]
    if latest.sleep_hours is not None:
        if latest.sleep_hours < 6:
            score -= 20
            parts.append({"factor": "sleep", "detail": f"睡眠 {latest.sleep_hours}h < 6h，-20"})
        elif latest.sleep_hours < 7:
            score -= 10
            parts.append({"factor": "sleep", "detail": f"睡眠 {latest.sleep_hours}h 不足 7h，-10"})
    if latest.fatigue_score is not None:
        if latest.fatigue_score >= 8:
            score -= 25
            parts.append({"factor": "fatigue", "detail": f"主观疲劳 {latest.fatigue_score}/10 ≥ 8，-25"})
        elif latest.fatigue_score >= 6:
            score -= 10
            parts.append({"factor": "fatigue", "detail": f"主观疲劳 {latest.fatigue_score}/10 ≥ 6，-10"})
    if latest.morning_resting_hr is not None:
        threshold = profile.resting_hr * 1.1
        if latest.morning_resting_hr > threshold:
            score -= 20
            parts.append({"factor": "resting_hr",
                          "detail": f"晨起静息心率 {latest.morning_resting_hr} 超过基线 {profile.resting_hr} 的 110%，-20"})
    return max(score, 0), parts
