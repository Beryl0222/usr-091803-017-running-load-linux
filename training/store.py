"""内存仓储与追加式事件日志。

负荷从不做增量缓存：每次需要时按有效记录（未被取代、已去重）现算，
因此漏传补录、跨日同步和手工修正都会自然反映到周计划中，而不会重复计数。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from .errors import NotFoundError
from .models import Event, Role


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self) -> None:
        self.runners = {}            # runner_id -> RunnerProfile
        self.sessions = {}           # record_id -> SessionRecord
        self.session_keys = {}       # (runner_id, source, external_id) -> record_id
        self.dailies = {}            # metrics_id -> DailyMetrics
        self.daily_keys = {}         # (runner_id, source, external_id) -> metrics_id
        self.processed_syncs = set() # (source, sync_id) 整包幂等
        self.recommendations = {}    # recommendation_id -> Recommendation
        self.events = []             # 追加式事件日志
        self.lock = threading.RLock()

    # ---- 跑者 ----
    def add_runner(self, profile) -> None:
        with self.lock:
            self.runners[profile.runner_id] = profile

    def get_runner(self, runner_id: str):
        profile = self.runners.get(runner_id)
        if profile is None:
            raise NotFoundError(f"跑者不存在: {runner_id}")
        return profile

    # ---- 记录查询 ----
    def sessions_for(self, runner_id: str, active_only: bool = True) -> list:
        records = [s for s in self.sessions.values() if s.runner_id == runner_id]
        if active_only:
            records = [s for s in records if s.superseded_by is None]
        return sorted(records, key=lambda s: s.started_at)

    def dailies_for(self, runner_id: str, active_only: bool = True) -> list:
        records = [d for d in self.dailies.values() if d.runner_id == runner_id]
        if active_only:
            records = [d for d in records if d.superseded_by is None]
        return sorted(records, key=lambda d: d.day)

    # ---- 建议 ----
    def add_recommendation(self, rec) -> None:
        with self.lock:
            self.recommendations[rec.recommendation_id] = rec

    def get_recommendation(self, recommendation_id: str):
        rec = self.recommendations.get(recommendation_id)
        if rec is None:
            raise NotFoundError(f"建议不存在: {recommendation_id}")
        return rec

    def recommendations_for(self, runner_id: str) -> list:
        recs = [r for r in self.recommendations.values() if r.runner_id == runner_id]
        return sorted(recs, key=lambda r: r.created_at)

    # ---- 事件 ----
    def log(self, type_: str, actor_id: str, actor_role: Role, entity_id: str,
            runner_id: str, details: dict, at: Optional[datetime] = None) -> Event:
        with self.lock:
            event = Event(
                seq=len(self.events) + 1,
                type=type_,
                actor_id=actor_id,
                actor_role=actor_role,
                at=at or utcnow(),
                entity_id=entity_id,
                runner_id=runner_id,
                details=details,
            )
            self.events.append(event)
            return event

    def events_for_runner(self, runner_id: str) -> list:
        return [e for e in self.events if e.runner_id == runner_id]

    def events_for_entity(self, entity_id: str) -> list:
        return [e for e in self.events if e.entity_id == entity_id]
