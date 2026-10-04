"""领域异常类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    status_code = 400
    code = "domain_error"


class ValidationError(DomainError):
    """请求内容不满足领域约束。"""

    status_code = 400
    code = "validation_error"

    def __init__(self, message: str, details: object = None) -> None:
        super().__init__(message)
        self.details = details


class NotFoundError(DomainError):
    """引用的对象不存在。"""

    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    """对象已存在或唯一约束冲突。"""

    status_code = 409
    code = "conflict"


class WorkflowError(DomainError):
    """操作不符合备案状态机。"""

    status_code = 409
    code = "workflow_error"
