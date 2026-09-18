"""领域错误：统一映射到 HTTP 状态码与错误码。"""

from __future__ import annotations


class DomainError(Exception):
    """业务规则违例。status/code 供 HTTP 层直接翻译。"""

    status = 400
    code = "domain_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(DomainError):
    status = 400
    code = "validation_error"


class UnauthorizedError(DomainError):
    status = 401
    code = "unauthorized"


class ForbiddenError(DomainError):
    status = 403
    code = "forbidden"


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"
