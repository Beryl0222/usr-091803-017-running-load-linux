"""设备数据摄入：幂等去重、手工修正、跨日归属与时区观察。

规则：
- 同一 (source, sync_id) 的同步包只处理一次，重复投递直接返回 duplicate；
- 同一 (runner, source, external_id) 的记录只计一次负荷；
- 手工修正通过 corrects 指回原记录：原记录保留并标记 superseded_by，负荷按新版本计算；
- 训练归属日期一律按跑者常驻时区换算，跨日到达的数据归回真实发生日，
  漏传晚到不会把负荷错记到到达日。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .errors import ValidationError
from .models import Actor, DailyMetrics, Role, SessionRecord, new_id
from .store import Store, utcnow


def parse_datetime(value, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} 必须是 ISO 8601 字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(f"{field_name} 不是合法的 ISO 8601 时间: {value}")
    if parsed.tzinfo is None:
        raise ValidationError(f"{field_name} 必须携带时区偏移: {value}")
    return parsed


def parse_date(value, field_name: str):
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} 必须是 YYYY-MM-DD 字符串")
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError(f"{field_name} 不是合法日期: {value}")


def _require_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"缺少必填字段: {key}")
    return value.strip()


def _optional_number(payload: dict, key: str):
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValidationError(f"字段 {key} 必须是数字")
    return value


def _optional_int(payload: dict, key: str):
    value = _optional_number(payload, key)
    return int(value) if value is not None else None


def ingest_sync(store: Store, envelope: dict, actor: Actor, now: Optional[datetime] = None) -> dict:
    """处理一个设备同步包，返回摄入结果（含去重与跨日信息）。"""
    now = now or utcnow()
    if not isinstance(envelope, dict):
        raise ValidationError("同步包必须是 JSON 对象")
    sync_id = _require_str(envelope, "sync_id")
    source = _require_str(envelope, "source")
    runner_id = _require_str(envelope, "runner_id")
    device_timezone = envelope.get("device_timezone") or None
    sessions = envelope.get("sessions") or []
    dailies = envelope.get("dailies") or []
    if not isinstance(sessions, list) or not isinstance(dailies, list):
        raise ValidationError("sessions/dailies 必须是数组")

    with store.lock:
        profile = store.get_runner(runner_id)
        home_tz = ZoneInfo(profile.home_timezone)

        sync_key = (source, sync_id)
        if sync_key in store.processed_syncs:
            store.log("sync_duplicate_ignored", actor.actor_id, actor.role, sync_id, runner_id,
                      {"source": source, "reason": "同一同步包重复投递，忽略"}, at=now)
            return {"status": "duplicate", "sync_id": sync_id, "source": source,
                    "ingested": 0, "duplicates": 0, "corrected": 0, "cross_day": []}

        result = {"status": "ok", "sync_id": sync_id, "source": source,
                  "ingested": 0, "duplicates": 0, "corrected": 0, "cross_day": []}

        for item in sessions:
            _ingest_session(store, profile, home_tz, source, item, device_timezone, now, result)
        for item in dailies:
            _ingest_daily(store, profile, source, item, now, result)

        if device_timezone:
            profile.last_device_timezone = device_timezone
        store.processed_syncs.add(sync_key)
        store.log("sync_ingested", actor.actor_id, actor.role, sync_id, runner_id,
                  {"source": source, "ingested": result["ingested"],
                   "duplicates": result["duplicates"], "corrected": result["corrected"],
                   "device_timezone": device_timezone}, at=now)
        return result


def _ingest_session(store: Store, profile, home_tz, source: str, item: dict,
                    device_timezone: Optional[str], now: datetime, result: dict) -> None:
    if not isinstance(item, dict):
        raise ValidationError("sessions 元素必须是对象")
    external_id = _require_str(item, "external_id")
    started_at = parse_datetime(item.get("started_at"), "started_at")
    duration = _optional_number(item, "duration_min")
    if duration is None or duration <= 0:
        raise ValidationError("duration_min 必须为正数")
    corrects = item.get("corrects")

    key = (profile.runner_id, source, external_id)
    if key in store.session_keys:
        result["duplicates"] += 1
        return

    home_date = started_at.astimezone(home_tz).date()
    record = SessionRecord(
        record_id=new_id("ses"),
        runner_id=profile.runner_id,
        source=source,
        external_id=external_id,
        started_at=started_at,
        duration_min=float(duration),
        home_date=home_date,
        device_timezone=device_timezone or profile.home_timezone,
        avg_hr=_optional_int(item, "avg_hr"),
        max_hr=_optional_int(item, "max_hr"),
        cadence_spm=_optional_int(item, "cadence_spm"),
        distance_km=_optional_number(item, "distance_km"),
        supersedes=corrects,
        ingested_at=now,
    )

    if corrects:
        original_id = store.session_keys.get((profile.runner_id, source, corrects))
        if original_id is None:
            raise ValidationError(f"corrects 指向的原始记录不存在: {corrects}")
        original = store.sessions[original_id]
        if original.superseded_by is not None:
            raise ValidationError(f"原始记录已被修正过: {corrects}")
        original.superseded_by = record.record_id
        store.log("record_corrected", "device-sync", Role.SYSTEM, record.record_id,
                  profile.runner_id,
                  {"original_record_id": original_id, "corrects": corrects}, at=now)
        result["corrected"] += 1

    tz_name = device_timezone or profile.home_timezone
    try:
        device_date = started_at.astimezone(ZoneInfo(tz_name)).date()
    except Exception:
        device_date = home_date
    if device_date != home_date:
        result["cross_day"].append({
            "external_id": external_id,
            "device_date": device_date.isoformat(),
            "home_date": home_date.isoformat(),
        })

    store.sessions[record.record_id] = record
    store.session_keys[key] = record.record_id
    result["ingested"] += 1


def _ingest_daily(store: Store, profile, source: str, item: dict,
                  now: datetime, result: dict) -> None:
    if not isinstance(item, dict):
        raise ValidationError("dailies 元素必须是对象")
    external_id = _require_str(item, "external_id")
    day = parse_date(item.get("day"), "day")
    corrects = item.get("corrects")

    key = (profile.runner_id, source, external_id)
    if key in store.daily_keys:
        result["duplicates"] += 1
        return

    metrics = DailyMetrics(
        metrics_id=new_id("dly"),
        runner_id=profile.runner_id,
        source=source,
        external_id=external_id,
        day=day,
        sleep_hours=_optional_number(item, "sleep_hours"),
        fatigue_score=_optional_int(item, "fatigue_score"),
        morning_resting_hr=_optional_int(item, "morning_resting_hr"),
        supersedes=corrects,
        ingested_at=now,
    )
    if metrics.fatigue_score is not None and not 1 <= metrics.fatigue_score <= 10:
        raise ValidationError("fatigue_score 必须在 1-10 之间")

    if corrects:
        original_id = store.daily_keys.get((profile.runner_id, source, corrects))
        if original_id is None:
            raise ValidationError(f"corrects 指向的原始记录不存在: {corrects}")
        original = store.dailies[original_id]
        if original.superseded_by is not None:
            raise ValidationError(f"原始记录已被修正过: {corrects}")
        original.superseded_by = metrics.metrics_id
        store.log("record_corrected", "device-sync", Role.SYSTEM, metrics.metrics_id,
                  profile.runner_id,
                  {"original_metrics_id": original_id, "corrects": corrects}, at=now)
        result["corrected"] += 1

    store.dailies[metrics.metrics_id] = metrics
    store.daily_keys[key] = metrics.metrics_id
    result["ingested"] += 1
