"""领域错误类型。"""


class DomainError(Exception):
    """领域规则冲突。"""


class UnknownProjectError(DomainError):
    """项目未登记。"""


class AmbiguousNameError(DomainError):
    """名称或别名对应多个项目。"""


class UnknownFilingError(DomainError):
    """备案申请不存在。"""


class InvalidStateError(DomainError):
    """当前状态不允许该操作。"""


class ReviewOrderError(DomainError):
    """复核角色顺序错误。"""
