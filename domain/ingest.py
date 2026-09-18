"""可穿戴数据汇集：去重、跨日归因与手工修正，保证负荷只计算一次。"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import NotFoundError, ValidationError
from .models import (
    DEFAULT_SLOW_JOG_INTENSITY,
    GROUP_LOAD_MULTIPLIER,
    MetricBatch,
    Sample,
    WorkoutRecord,
    parse_ts,
    utcnow,
)

VALID_SOURCES = ("device", "manual")


class IngestService:
    """把设备批次规范化成训练记录；重复同步与手工修正都不会重复计负荷。"""

    def __init__(self, store):
        self.store = store

    def ingest(self, user_id, sync_id, device_id=None, tz=None, samples=None,
               source="device", corrects=None, now=None):
        now = now or utcnow()
        with self.store.lock:
            user = self.store.users.get(user_id)
            if user is None:
                raise NotFoundError(f"用户不存在：{user_id}")
            if not sync_id:
                raise ValidationError("缺少同步批次号 sync_id")
            if source not in VALID_SOURCES:
                raise ValidationError(f"未知数据来源：{source}")
            tz = tz or user.home_tz
            try:
                zone = ZoneInfo(tz)
            except (ZoneInfoNotFoundError, KeyError, ValueError, TypeError):
                raise ValidationError(f"无法识别的时区：{tz}")

            key = (user_id, sync_id)
            if key in self.store.batches:
                # 设备重复同步：直接忽略，负荷只计算一次。
                self.store.emit("device", "metrics_duplicate_ignored",
                                {"user_id": user_id, "sync_id": sync_id}, now)
                return {"status": "duplicate", "sync_id": sync_id, "records": [],
                        "corrected_records": []}

            parsed = [self._parse_sample(s) for s in (samples or [])]
            batch = MetricBatch(sync_id=sync_id, user_id=user_id,
                                device_id=device_id or "unknown", tz=tz,
                                source=source, corrects=corrects,
                                synced_at=now, samples=parsed)
            self.store.batches[key] = batch

            corrected = self._supersede(user_id, corrects, sync_id, now) if corrects else []

            records = [self._to_record(user, batch, sample, zone)
                       for sample in parsed if sample.kind == "workout"]
            for record in records:
                self.store.records[record.record_id] = record

            if tz != user.home_tz:
                # 时区变化不在这里处理，交给规则引擎在下一次建议时暂停并请求确认。
                user.last_seen_tz = tz

            self.store.emit("device", "metrics_ingested", {
                "user_id": user_id, "sync_id": sync_id, "source": source,
                "records": [r.record_id for r in records],
                "corrected_records": corrected,
                "total_load": round(sum(r.load for r in records), 2),
            }, now)
            return {"status": "accepted", "sync_id": sync_id,
                    "records": [r.record_id for r in records],
                    "corrected_records": corrected}

    def _parse_sample(self, raw) -> Sample:
        if not isinstance(raw, dict) or "kind" not in raw or "recorded_at" not in raw:
            raise ValidationError("样本需要包含 kind 与 recorded_at")
        kind = raw["kind"]
        if kind not in ("workout", "heart_rate", "sleep", "fatigue"):
            raise ValidationError(f"未知样本类型：{kind}")
        value = raw.get("value") or {}
        if kind == "workout":
            duration = value.get("duration_min")
            if not isinstance(duration, (int, float)) or duration <= 0:
                raise ValidationError("训练样本需要正的 duration_min")
        return Sample(kind=kind, recorded_at=parse_ts(raw["recorded_at"]), value=value)

    def _supersede(self, user_id, corrects, sync_id, now):
        """手工修正：作废旧批次的记录，负荷仍只计算一次。"""
        original = self.store.batches.get((user_id, corrects))
        if original is None:
            raise ValidationError(f"要修正的原始批次不存在：{corrects}")
        original.status = "superseded"
        superseded = []
        for record in self.store.records.values():
            if record.user_id == user_id and record.sync_id == corrects \
                    and record.status == "active":
                record.status = "superseded"
                record.corrected_by = sync_id
                superseded.append(record.record_id)
        self.store.emit("manual", "metric_corrected", {
            "user_id": user_id, "original_sync_id": corrects,
            "correction_sync_id": sync_id, "superseded_records": superseded,
        }, now)
        return superseded

    def _to_record(self, user, batch, sample, zone) -> WorkoutRecord:
        value = sample.value
        local_date = sample.recorded_at.astimezone(zone).date().isoformat()
        avg_hr = value.get("avg_hr")
        intensity = (avg_hr / user.max_hr) if avg_hr else DEFAULT_SLOW_JOG_INTENSITY
        load = round(value["duration_min"] * intensity
                     * GROUP_LOAD_MULTIPLIER[user.group], 2)
        return WorkoutRecord(
            record_id=self.store.new_id("rec"),
            user_id=user.user_id,
            sync_id=batch.sync_id,
            recorded_at=sample.recorded_at,
            local_date=local_date,
            duration_min=float(value["duration_min"]),
            avg_hr=float(avg_hr) if avg_hr else None,
            avg_cadence=value.get("avg_cadence"),
            distance_km=value.get("distance_km"),
            load=load,
            source=batch.source,
        )

    def active_records(self, user_id):
        return [r for r in self.store.records.values()
                if r.user_id == user_id and r.status == "active"]

    def wellness_samples(self, user_id, kind, since):
        """取自未被修正取代的批次，避免手工修正前的旧值再次生效。"""
        out = []
        for (uid, _), batch in self.store.batches.items():
            if uid != user_id or batch.status != "accepted":
                continue
            for sample in batch.samples:
                if sample.kind == kind and sample.recorded_at >= since:
                    out.append(sample)
        return out
