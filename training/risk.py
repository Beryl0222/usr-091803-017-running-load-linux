"""风险闸门：异常心率、伤病标签、时区变化、关键数据缺失。

任一信号出现时，建议不得自动增加训练量，相关安排暂停，
等待本人或专业人员确认后才恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .load import RECENT_WINDOW_DAYS
from .models import RiskFlag, RunnerProfile

# 异常心率判定阈值
SESSION_MAX_HR_RATIO = 0.9    # 单次最高心率 ≥ 90% 最大心率
RESTING_HR_RATIO = 1.1        # 晨起静息心率 ≥ 110% 基线静息心率
RESTING_HR_LOOKBACK_DAYS = 3


@dataclass
class RiskAssessment:
    flags: list = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.flags

    def to_dict(self) -> dict:
        return {"flags": [f.value for f in self.flags], "details": self.details}


def assess(store, profile: RunnerProfile, today: date) -> RiskAssessment:
    result = RiskAssessment()
    start_day = today - timedelta(days=RECENT_WINDOW_DAYS - 1)

    sessions = [s for s in store.sessions_for(profile.runner_id)
                if start_day <= s.home_date <= today]
    dailies = [d for d in store.dailies_for(profile.runner_id)
               if start_day <= d.day <= today]

    # 1) 异常心率：单次最高心率越限，或近几日晨起静息心率明显高于基线。
    hr_hits = []
    for record in sessions:
        if record.max_hr is not None and record.max_hr >= SESSION_MAX_HR_RATIO * profile.max_hr:
            hr_hits.append({"kind": "session_max_hr", "external_id": record.external_id,
                            "max_hr": record.max_hr,
                            "threshold": round(SESSION_MAX_HR_RATIO * profile.max_hr, 1),
                            "day": record.home_date.isoformat()})
    resting_threshold = RESTING_HR_RATIO * profile.resting_hr
    for daily in dailies:
        if daily.day < today - timedelta(days=RESTING_HR_LOOKBACK_DAYS - 1):
            continue
        if daily.morning_resting_hr is not None and daily.morning_resting_hr >= resting_threshold:
            hr_hits.append({"kind": "morning_resting_hr", "day": daily.day.isoformat(),
                            "morning_resting_hr": daily.morning_resting_hr,
                            "threshold": round(resting_threshold, 1)})
    if hr_hits:
        result.flags.append(RiskFlag.ABNORMAL_HEART_RATE)
        result.details["abnormal_heart_rate"] = hr_hits

    # 2) 伤病标签：存在仍处于活动状态的标签。
    active_injuries = profile.active_injuries()
    if active_injuries:
        result.flags.append(RiskFlag.ACTIVE_INJURY)
        result.details["active_injury"] = [t.to_dict() for t in active_injuries]

    # 3) 时区变化：最近同步上报的设备时区与常驻时区不一致，
    #    跨日归属可能失真，需人工确认后再加量。
    if profile.last_device_timezone and profile.last_device_timezone != profile.home_timezone:
        result.flags.append(RiskFlag.TIMEZONE_CHANGE)
        result.details["timezone_change"] = {
            "home_timezone": profile.home_timezone,
            "device_timezone": profile.last_device_timezone,
        }

    # 4) 关键数据缺失：窗口内有缺心率的训练记录，
    #    或有训练记录却完全没有睡眠/疲劳等每日指标。
    missing = []
    no_hr = [s for s in sessions if s.avg_hr is None]
    if no_hr:
        missing.append({"kind": "session_without_heart_rate",
                        "external_ids": [s.external_id for s in no_hr]})
    if sessions and not dailies:
        missing.append({"kind": "no_daily_metrics",
                        "window_days": RECENT_WINDOW_DAYS})
    if missing:
        result.flags.append(RiskFlag.MISSING_KEY_DATA)
        result.details["missing_key_data"] = missing

    return result
