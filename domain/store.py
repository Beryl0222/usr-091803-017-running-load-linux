"""内存仓储与追加式事件日志：所有状态变化都会留下可追溯事件。"""

from __future__ import annotations

import threading
from uuid import uuid4

from .models import Event, utcnow


class Store:
    """集中保存领域对象；HTTP 层与测试共用一个实例。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.users = {}             # user_id -> User
        self.batches = {}           # (user_id, sync_id) -> MetricBatch
        self.records = {}           # record_id -> WorkoutRecord
        self.injuries = []          # list[InjuryTag]
        self.recommendations = {}   # rec_id -> Recommendation
        self.plan_versions = {}     # user_id -> list[PlanVersion]
        self.plan_status = {}       # user_id -> {"status", "paused_by"}
        self.grants = []            # list[Grant]
        self.events = []            # list[Event]，只追加不修改

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid4().hex[:12]}"

    def emit(self, actor: str, type_: str, payload: dict, ts=None) -> Event:
        event = Event(event_id=self.new_id("evt"), ts=ts or utcnow(),
                      actor=actor, type=type_, payload=payload)
        self.events.append(event)
        return event
