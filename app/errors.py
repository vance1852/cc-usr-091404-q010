"""领域异常：所有业务校验失败都转换为带中文说明的 HTTP 响应。"""
from __future__ import annotations


class DomainError(Exception):
    status_code = 400
    code = "DOMAIN_ERROR"

    def __init__(self, message: str, code: str | None = None, details: dict | None = None,
                 status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details or {}


class NotFoundError(DomainError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(DomainError):
    status_code = 409
    code = "CONFLICT"


class InvariantViolation(DomainError):
    """守恒 / 状态机 / 保管唯一性等不变量被破坏。"""
    status_code = 400
    code = "INVARIANT_VIOLATION"
