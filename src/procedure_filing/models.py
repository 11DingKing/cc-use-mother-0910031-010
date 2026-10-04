"""医美项目分级备案的领域模型与词汇。"""
from __future__ import annotations

from dataclasses import dataclass, field


class Role:
    """参与目录维护与复核的角色，与 domain/contract.json 的 actors 对齐。"""

    COMPLIANCE = "机构合规员"
    PRACTITIONER = "执业人员"
    REGULATOR = "监管人员"
    EXPERT = "复核专家"

    ALL = (COMPLIANCE, PRACTITIONER, REGULATOR, EXPERT)


class FilingState:
    """备案申请状态机，与 domain/contract.json 的 states 对齐。"""

    REGISTERED = "登记"
    PENDING = "待核验"
    REVIEWING = "处置中"
    DECIDED = "已决定"
    ARCHIVED = "已归档"

    ALL = (REGISTERED, PENDING, REVIEWING, DECIDED, ARCHIVED)


class ChangeType:
    """分类版本链上的变更类型。"""

    INITIAL = "初始登记"
    CORRECTION = "分类更正"
    SPLIT = "项目拆分"
    SUBSTITUTION = "依赖替代"
    SUSPENSION = "紧急暂停"

    ALL = (INITIAL, CORRECTION, SPLIT, SUBSTITUTION, SUSPENSION)


class VersionStatus:
    """分类版本生效状态。"""

    ACTIVE = "生效"
    SUSPENDED = "暂停"
    SPLIT = "已拆分"


class Decision:
    """监管决定。"""

    APPROVED = "批准"
    REJECTED = "驳回"


# 多角色复核顺序：执业人员确认执业条件，复核专家把关分类适用，监管人员作出决定。
REVIEW_SEQUENCE = (Role.PRACTITIONER, Role.EXPERT, Role.REGULATOR)

# 目录维护（登记、更正、拆分、依赖替代）允许的角色；紧急暂停仅限监管人员。
CATALOG_EDITORS = (Role.REGULATOR, Role.EXPERT)

# 已知风险等级的排序，用于差异比较时判断上调/下调。
RISK_ORDER = {"一级": 1, "二级": 2, "三级": 3, "四级": 4}


@dataclass(frozen=True)
class VenueRequirement:
    """场地要求：房间类型与最小面积。"""

    room_type: str
    min_area_sqm: float


@dataclass(frozen=True)
class PersonnelRequirement:
    """必备人员：岗位、资质与最少人数。"""

    role: str
    qualification: str
    min_count: int = 1


@dataclass(frozen=True)
class EquipmentRequirement:
    """必备设备：名称与最少台数。"""

    equipment: str
    min_count: int = 1


@dataclass(frozen=True)
class DeclaredPersonnel:
    """机构申报的一名人员及其资质。"""

    role: str
    qualification: str


@dataclass(frozen=True)
class ClassificationVersion:
    """项目分类的一个版本，版本链上的节点，创建后不可变。"""

    project_id: str
    version_no: int
    risk_level: str
    venue: VenueRequirement | None
    personnel: tuple[PersonnelRequirement, ...]
    equipment: tuple[EquipmentRequirement, ...]
    status: str
    change_type: str
    change_reason: str
    parent_version: int | None
    source_project_id: str | None
    created_by: str
    seq: int


@dataclass
class Project:
    """诊疗项目：规范名称与别名集合。状态由当前分类版本派生。"""

    project_id: str
    name: str
    aliases: set[str] = field(default_factory=set)
    split_into: tuple[str, ...] = ()


@dataclass(frozen=True)
class Review:
    """一次复核意见。"""

    round_no: int
    role: str
    outcome: str
    note: str
    seq: int


@dataclass
class Filing:
    """备案申请：提交时钉住分类版本快照，决定后冻结。"""

    filing_id: str
    institution_id: str
    project_id: str
    classification_version: int
    snapshot: ClassificationVersion
    declared_equipment: dict[str, int]
    declared_personnel: tuple[DeclaredPersonnel, ...]
    declared_venue: VenueRequirement | None
    state: str
    issues: tuple[str, ...]
    reviews: tuple[Review, ...]
    review_round: int
    decision: str | None
    seq: int


@dataclass(frozen=True)
class ServiceRecord:
    """历史服务记录：始终指向服务发生当时备案所钉住的分类版本。"""

    record_id: str
    filing_id: str
    institution_id: str
    project_id: str
    classification_version: int
    seq: int
