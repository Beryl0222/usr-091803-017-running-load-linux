"""测试共享构造工具：固定时钟与标准档案/同步包。"""

from datetime import datetime, timedelta, timezone

from training.models import Actor, InjuryTag, PopulationGroup, Role, RunnerProfile
from training.store import Store

CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 18, 10, 0, tzinfo=CN)  # 周五，目标周为下一 ISO 周
SYSTEM = Actor("system", Role.SYSTEM)


def make_store():
    return Store()


def add_profile(store, runner_id="r1", groups=(PopulationGroup.GENERAL,),
                resting_hr=60, max_hr=180, baseline=100.0, injuries=(),
                home_timezone="Asia/Shanghai"):
    profile = RunnerProfile(
        runner_id=runner_id,
        resting_hr=resting_hr,
        max_hr=max_hr,
        baseline_weekly_load=baseline,
        groups=tuple(groups),
        home_timezone=home_timezone,
        injury_tags=[InjuryTag(label=t, noted_by="test", noted_at=NOW) for t in injuries],
        created_at=NOW,
    )
    store.add_runner(profile)
    return profile


def session(external_id, day, duration=30.0, avg_hr=120, max_hr=None,
            cadence=170, distance=3.0, start="07:00:00", offset="+08:00",
            corrects=None):
    item = {
        "external_id": external_id,
        "started_at": f"{day}T{start}{offset}",
        "duration_min": duration,
        "avg_hr": avg_hr,
        "max_hr": max_hr,
        "cadence_spm": cadence,
        "distance_km": distance,
        "corrects": corrects,
    }
    return {k: v for k, v in item.items() if v is not None}


def daily(external_id, day, sleep=7.5, fatigue=3, morning_hr=60, corrects=None):
    item = {
        "external_id": external_id,
        "day": day,
        "sleep_hours": sleep,
        "fatigue_score": fatigue,
        "morning_resting_hr": morning_hr,
        "corrects": corrects,
    }
    return {k: v for k, v in item.items() if v is not None}


def envelope(sync_id, runner_id, sessions=(), dailies=(),
             device_timezone="Asia/Shanghai", source="watch-1"):
    return {
        "sync_id": sync_id,
        "source": source,
        "runner_id": runner_id,
        "device_timezone": device_timezone,
        "sessions": list(sessions),
        "dailies": list(dailies),
    }


def three_easy_sessions():
    """最近 7 天内三次 30 分钟、强度 0.5 的超慢跑。"""
    return [
        session("s1", "2026-09-15"),
        session("s2", "2026-09-16"),
        session("s3", "2026-09-17"),
    ]
