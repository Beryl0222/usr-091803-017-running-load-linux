"""慢跑训练负荷服务：领域包。

模块划分：
- models：跑者、设备记录、建议、覆盖、事件等领域对象；
- store：内存仓储与追加式事件日志；
- ingestion：设备同步摄入（幂等去重、手工修正、跨日归属）；
- load：个性化负荷与恢复状态计算；
- risk：风险闸门（异常心率/伤病/时区变化/关键数据缺失）；
- recommendation：建议生成、暂停确认、教练覆盖与回放；
- access：角色访问控制与授权摘要投影；
- profiles：跑者注册与授权管理。
"""

from .errors import (ConflictError, DomainError, ForbiddenError, NotFoundError,
                     UnauthorizedError, ValidationError)
from .models import (Actor, PopulationGroup, RecommendationStatus, RiskFlag, Role)
from .store import Store

__all__ = [
    "Actor", "ConflictError", "DomainError", "ForbiddenError", "NotFoundError",
    "PopulationGroup", "RecommendationStatus", "RiskFlag", "Role", "Store",
    "UnauthorizedError", "ValidationError",
]
