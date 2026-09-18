"""领域错误类型：接口层据此映射 HTTP 状态码。"""


class DomainError(Exception):
    """领域错误基类。"""

    status = 400


class ValidationError(DomainError):
    """输入不完整或不合法。"""

    status = 400


class NotFoundError(DomainError):
    """目标对象不存在。"""

    status = 404


class ForbiddenError(DomainError):
    """无权访问或越权操作。"""

    status = 403


class ConflictError(DomainError):
    """状态冲突，例如重复确认、重复授权。"""

    status = 409
