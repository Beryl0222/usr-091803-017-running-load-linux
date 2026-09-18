"""慢跑训练负荷领域核心。"""

from .access import AccessService
from .errors import ConflictError, DomainError, ForbiddenError, NotFoundError, ValidationError
from .ingest import IngestService
from .recommend import RecommendationService
from .store import Store
from .users import DirectoryService


class TrainingService:
    """组合各领域服务的门面，供 HTTP 层与测试使用。"""

    def __init__(self):
        self.store = Store()
        self.directory = DirectoryService(self.store)
        self.ingest = IngestService(self.store)
        self.recommend = RecommendationService(self.store, self.ingest)
        self.access = AccessService(self.store, self.recommend)


__all__ = [
    "AccessService",
    "ConflictError",
    "DirectoryService",
    "DomainError",
    "ForbiddenError",
    "IngestService",
    "NotFoundError",
    "RecommendationService",
    "Store",
    "TrainingService",
    "ValidationError",
]
