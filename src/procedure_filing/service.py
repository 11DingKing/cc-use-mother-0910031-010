"""医美项目分级备案后端服务。

覆盖项目目录与别名、分类版本链（更正/拆分/依赖替代/紧急暂停）、
备案完整性检查与多角色复核、历史服务冻结以及规则影响分析。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from .analysis import diff_classification
from .errors import (
    AmbiguousNameError,
    DomainError,
    InvalidStateError,
    ReviewOrderError,
    UnknownFilingError,
    UnknownProjectError,
)
from .models import (
    CATALOG_EDITORS,
    REVIEW_SEQUENCE,
    ChangeType,
    ClassificationVersion,
    Decision,
    DeclaredPersonnel,
    EquipmentRequirement,
    Filing,
    FilingState,
    PersonnelRequirement,
    Project,
    Review,
    Role,
    ServiceRecord,
    VenueRequirement,
    VersionStatus,
)

# 区分“保持原值”与“置空”的哨兵。
_UNSET = object()

# 各复核角色允许给出的结论。
_OUTCOMES = {
    Role.PRACTITIONER: ("通过", "退回"),
    Role.EXPERT: ("通过", "退回"),
    Role.REGULATOR: (Decision.APPROVED, Decision.REJECTED, "退回"),
}


class FilingService:
    """项目目录、分类版本链、备案复核与服务记录的内存后端。"""

    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}
        self._versions: dict[str, list[ClassificationVersion]] = {}
        self._filings: dict[str, Filing] = {}
        self._services: dict[str, ServiceRecord] = {}
        self._name_index: dict[str, str] = {}
        self._counters = {"project": 0, "filing": 0, "service": 0}
        self._seq = 0

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _tick(self) -> int:
        self._seq += 1
        return self._seq

    def _next_id(self, kind: str, prefix: str) -> str:
        self._counters[kind] += 1
        return f"{prefix}-{self._counters[kind]:04d}"

    @staticmethod
    def _normalize(text: str) -> str:
        return str(text).strip()

    @staticmethod
    def _require_actor(actor: str, allowed: tuple[str, ...]) -> None:
        if actor not in allowed:
            raise DomainError(f"角色「{actor}」无权执行该操作，需要：{'、'.join(allowed)}")

    def _check_terms_free(self, terms: Iterable[str]) -> None:
        for term in terms:
            if term in self._name_index:
                raise DomainError(f"名称或别名已被占用：{term}")

    def _get_project(self, project_id: str) -> Project:
        try:
            return self._projects[project_id]
        except KeyError:
            raise UnknownProjectError(f"项目不存在：{project_id}") from None

    def _get_filing(self, filing_id: str) -> Filing:
        try:
            return self._filings[filing_id]
        except KeyError:
            raise UnknownFilingError(f"备案申请不存在：{filing_id}") from None

    # ------------------------------------------------------------------
    # 项目目录与别名
    # ------------------------------------------------------------------

    def register_project(
        self,
        name: str,
        *,
        aliases: Iterable[str] = (),
        risk_level: str,
        venue: VenueRequirement | None = None,
        personnel: Iterable[PersonnelRequirement] = (),
        equipment: Iterable[EquipmentRequirement] = (),
        actor: str,
        reason: str = "目录初始登记",
    ) -> Project:
        """登记新诊疗项目并生成首个分类版本。"""
        return self._register(
            name,
            aliases=aliases,
            risk_level=risk_level,
            venue=venue,
            personnel=personnel,
            equipment=equipment,
            actor=actor,
            change_type=ChangeType.INITIAL,
            reason=reason,
            source_project_id=None,
        )

    def _register(
        self,
        name: str,
        *,
        aliases: Iterable[str],
        risk_level: str,
        venue: VenueRequirement | None,
        personnel: Iterable[PersonnelRequirement],
        equipment: Iterable[EquipmentRequirement],
        actor: str,
        change_type: str,
        reason: str,
        source_project_id: str | None,
    ) -> Project:
        self._require_actor(actor, CATALOG_EDITORS)
        key = self._normalize(name)
        if not key:
            raise DomainError("项目名称不能为空")
        if not self._normalize(risk_level):
            raise DomainError("风险等级不能为空")
        alias_keys = {self._normalize(a) for a in aliases}
        alias_keys.discard("")
        self._check_terms_free([key, *sorted(alias_keys)])

        project = Project(project_id=self._next_id("project", "PRJ"), name=key, aliases=set(alias_keys))
        self._projects[project.project_id] = project
        self._name_index[key] = project.project_id
        for alias in alias_keys:
            self._name_index[alias] = project.project_id
        version = ClassificationVersion(
            project_id=project.project_id,
            version_no=1,
            risk_level=self._normalize(risk_level),
            venue=venue,
            personnel=tuple(personnel),
            equipment=tuple(equipment),
            status=VersionStatus.ACTIVE,
            change_type=change_type,
            change_reason=reason,
            parent_version=None,
            source_project_id=source_project_id,
            created_by=actor,
            seq=self._tick(),
        )
        self._versions[project.project_id] = [version]
        return project

    def add_alias(self, project_id: str, alias: str, *, actor: str) -> Project:
        """为项目补充别名；别名全局唯一，防止名称相近的项目混淆。"""
        self._require_actor(actor, CATALOG_EDITORS)
        project = self._get_project(project_id)
        key = self._normalize(alias)
        if not key:
            raise DomainError("别名不能为空")
        self._check_terms_free([key])
        project.aliases.add(key)
        self._name_index[key] = project.project_id
        return project

    def resolve_project(self, name_or_alias: str) -> Project:
        """按规范名称或别名精确解析项目；名称相近不会互相命中。"""
        key = self._normalize(name_or_alias)
        project_id = self._name_index.get(key)
        if project_id is None:
            raise UnknownProjectError(f"未登记的项目：{name_or_alias}")
        return self._projects[project_id]

    def find_similar(self, text: str) -> list[Project]:
        """列出名称或别名与给定文本相近的项目，提示它们可能适用不同条件。"""
        key = self._normalize(text)
        if not key:
            return []
        hits = {
            project_id
            for term, project_id in self._name_index.items()
            if key in term or term in key
        }
        return sorted((self._projects[i] for i in hits), key=lambda p: p.project_id)

    def get_project(self, project_id: str) -> Project:
        return self._get_project(project_id)

    def list_projects(self) -> list[Project]:
        return [self._projects[k] for k in sorted(self._projects)]

    # ------------------------------------------------------------------
    # 分类版本链
    # ------------------------------------------------------------------

    def current_version(self, project_id: str) -> ClassificationVersion:
        self._get_project(project_id)
        return self._versions[project_id][-1]

    def get_version(self, project_id: str, version_no: int) -> ClassificationVersion:
        self._get_project(project_id)
        for version in self._versions[project_id]:
            if version.version_no == version_no:
                return version
        raise DomainError(f"项目 {project_id} 不存在版本 {version_no}")

    def version_chain(self, project_id: str) -> list[ClassificationVersion]:
        """返回项目的完整版本链，按版本号升序。"""
        self._get_project(project_id)
        return list(self._versions[project_id])

    def current_classification(self, name_or_alias: str) -> ClassificationVersion:
        """按名称或别名查询当前生效分类：风险等级、场地要求与必备人员。"""
        return self.current_version(self.resolve_project(name_or_alias).project_id)

    def _append_version(
        self,
        project_id: str,
        *,
        risk_level: str,
        venue: VenueRequirement | None,
        personnel: Iterable[PersonnelRequirement],
        equipment: Iterable[EquipmentRequirement],
        status: str,
        change_type: str,
        reason: str,
        actor: str,
    ) -> ClassificationVersion:
        parent = self.current_version(project_id)
        version = ClassificationVersion(
            project_id=project_id,
            version_no=parent.version_no + 1,
            risk_level=risk_level,
            venue=venue,
            personnel=tuple(personnel),
            equipment=tuple(equipment),
            status=status,
            change_type=change_type,
            change_reason=reason,
            parent_version=parent.version_no,
            source_project_id=parent.source_project_id,
            created_by=actor,
            seq=self._tick(),
        )
        self._versions[project_id].append(version)
        return version

    def _require_active(self, project_id: str, action: str) -> ClassificationVersion:
        current = self.current_version(project_id)
        if current.status != VersionStatus.ACTIVE:
            raise InvalidStateError(f"项目当前为「{current.status}」，不能{action}")
        return current

    def correct_classification(
        self,
        project_id: str,
        *,
        actor: str,
        reason: str,
        risk_level: str | None = None,
        venue: VenueRequirement | None | object = _UNSET,
        personnel: Iterable[PersonnelRequirement] | None = None,
        equipment: Iterable[EquipmentRequirement] | None = None,
    ) -> ClassificationVersion:
        """分类更正：以新版本修正风险等级、场地或依赖，旧版本保留在链上。"""
        self._require_actor(actor, CATALOG_EDITORS)
        current = self._require_active(project_id, "更正分类")
        if not self._normalize(reason):
            raise DomainError("分类更正必须说明理由")
        return self._append_version(
            project_id,
            risk_level=self._normalize(risk_level) if risk_level else current.risk_level,
            venue=current.venue if venue is _UNSET else venue,
            personnel=current.personnel if personnel is None else tuple(personnel),
            equipment=current.equipment if equipment is None else tuple(equipment),
            status=VersionStatus.ACTIVE,
            change_type=ChangeType.CORRECTION,
            reason=reason,
            actor=actor,
        )

    def substitute_dependency(
        self,
        project_id: str,
        *,
        actor: str,
        reason: str,
        remove_equipment: Iterable[str] = (),
        add_equipment: Iterable[EquipmentRequirement] = (),
        remove_personnel: Iterable[tuple[str, str]] = (),
        add_personnel: Iterable[PersonnelRequirement] = (),
    ) -> ClassificationVersion:
        """依赖替代：以新设备/人员要求替换旧要求，形成新版本。

        remove_equipment 按设备名移除，remove_personnel 按 (岗位, 资质) 移除；
        移除目标不存在时拒绝操作，防止误删。
        """
        self._require_actor(actor, CATALOG_EDITORS)
        current = self._require_active(project_id, "替代依赖")
        if not self._normalize(reason):
            raise DomainError("依赖替代必须说明理由")

        remove_eq = {self._normalize(e) for e in remove_equipment}
        existing_eq = {e.equipment for e in current.equipment}
        missing_eq = remove_eq - existing_eq
        if missing_eq:
            raise DomainError(f"待移除设备不存在：{'、'.join(sorted(missing_eq))}")
        remove_ps = {(self._normalize(r), self._normalize(q)) for r, q in remove_personnel}
        existing_ps = {(p.role, p.qualification) for p in current.personnel}
        missing_ps = remove_ps - existing_ps
        if missing_ps:
            raise DomainError("待移除人员要求不存在：" + "、".join(f"{r}/{q}" for r, q in sorted(missing_ps)))

        equipment = [e for e in current.equipment if e.equipment not in remove_eq]
        equipment.extend(add_equipment)
        personnel = [p for p in current.personnel if (p.role, p.qualification) not in remove_ps]
        personnel.extend(add_personnel)
        return self._append_version(
            project_id,
            risk_level=current.risk_level,
            venue=current.venue,
            personnel=personnel,
            equipment=equipment,
            status=VersionStatus.ACTIVE,
            change_type=ChangeType.SUBSTITUTION,
            reason=reason,
            actor=actor,
        )

    def emergency_suspend(self, project_id: str, *, actor: str, reason: str) -> ClassificationVersion:
        """紧急暂停：仅监管人员可发起；暂停后停止新备案、复核与服务登记。"""
        self._require_actor(actor, (Role.REGULATOR,))
        current = self._require_active(project_id, "紧急暂停")
        if not self._normalize(reason):
            raise DomainError("紧急暂停必须说明理由")
        return self._append_version(
            project_id,
            risk_level=current.risk_level,
            venue=current.venue,
            personnel=current.personnel,
            equipment=current.equipment,
            status=VersionStatus.SUSPENDED,
            change_type=ChangeType.SUSPENSION,
            reason=reason,
            actor=actor,
        )

    def split_project(
        self,
        project_id: str,
        *,
        actor: str,
        reason: str,
        splits: Iterable[Mapping[str, object]],
    ) -> list[Project]:
        """项目拆分：原项目以「已拆分」版本收尾，每个子项目生成新的版本链。

        splits 中每项需包含 name、risk_level，可选 aliases、venue、personnel、equipment。
        """
        self._require_actor(actor, CATALOG_EDITORS)
        current = self._require_active(project_id, "拆分项目")
        if not self._normalize(reason):
            raise DomainError("项目拆分必须说明理由")
        specs = list(splits)
        if len(specs) < 2:
            raise DomainError("项目拆分至少需要两个子项目")

        terms: list[str] = []
        for spec in specs:
            child_name = self._normalize(str(spec.get("name", "")))
            if not child_name:
                raise DomainError("拆分方案缺少子项目名称")
            if not self._normalize(str(spec.get("risk_level", ""))):
                raise DomainError(f"子项目 {child_name} 缺少风险等级")
            terms.append(child_name)
            terms.extend(self._normalize(str(a)) for a in spec.get("aliases", ()))  # type: ignore[union-attr]
        terms = [t for t in terms if t]
        if len(set(terms)) != len(terms):
            raise DomainError("拆分方案内部名称或别名重复")
        self._check_terms_free(terms)

        children = [
            self._register(
                str(spec["name"]),
                aliases=spec.get("aliases", ()),  # type: ignore[arg-type]
                risk_level=str(spec["risk_level"]),
                venue=spec.get("venue"),  # type: ignore[arg-type]
                personnel=spec.get("personnel", ()),  # type: ignore[arg-type]
                equipment=spec.get("equipment", ()),  # type: ignore[arg-type]
                actor=actor,
                change_type=ChangeType.SPLIT,
                reason=reason,
                source_project_id=project_id,
            )
            for spec in specs
        ]
        self._append_version(
            project_id,
            risk_level=current.risk_level,
            venue=current.venue,
            personnel=current.personnel,
            equipment=current.equipment,
            status=VersionStatus.SPLIT,
            change_type=ChangeType.SPLIT,
            reason=reason,
            actor=actor,
        )
        self._get_project(project_id).split_into = tuple(c.project_id for c in children)
        return children

    # ------------------------------------------------------------------
    # 备案申请：完整性检查 + 多角色复核
    # ------------------------------------------------------------------

    @staticmethod
    def _check_completeness(
        version: ClassificationVersion,
        equipment_counts: Mapping[str, int],
        personnel: tuple[DeclaredPersonnel, ...],
        venue: VenueRequirement | None,
    ) -> tuple[str, ...]:
        """依赖完整性检查：设备、人员、场地逐项比对，返回问题清单。"""
        issues: list[str] = []
        for req in version.equipment:
            have = equipment_counts.get(req.equipment, 0)
            if have < req.min_count:
                issues.append(f"缺少设备：{req.equipment}（需要{req.min_count}台，已申报{have}台）")
        staffed: dict[tuple[str, str], int] = {}
        for person in personnel:
            key = (person.role, person.qualification)
            staffed[key] = staffed.get(key, 0) + 1
        for req in version.personnel:
            have = staffed.get((req.role, req.qualification), 0)
            if have < req.min_count:
                issues.append(
                    f"人员不足：{req.role}需{req.min_count}名具备「{req.qualification}」，已申报{have}名"
                )
        required_venue = version.venue
        if required_venue is not None:
            if venue is None:
                issues.append(f"缺少场地：{required_venue.room_type}")
            elif venue.room_type != required_venue.room_type:
                issues.append(f"场地类型不符：需要{required_venue.room_type}，已申报{venue.room_type}")
            elif venue.min_area_sqm < required_venue.min_area_sqm:
                issues.append(
                    f"场地面积不足：{required_venue.room_type}需≥{required_venue.min_area_sqm:g}㎡，"
                    f"已申报{venue.min_area_sqm:g}㎡"
                )
        return tuple(issues)

    def submit_filing(
        self,
        institution_id: str,
        *,
        project_name: str,
        actor: str,
        declared_equipment: Mapping[str, int] | None = None,
        declared_personnel: Iterable[DeclaredPersonnel] = (),
        declared_venue: VenueRequirement | None = None,
    ) -> Filing:
        """提交备案申请：先运行完整性检查，通过则进入待核验，否则留在登记状态。"""
        self._require_actor(actor, (Role.COMPLIANCE,))
        institution = self._normalize(institution_id)
        if not institution:
            raise DomainError("机构编号不能为空")
        project = self.resolve_project(project_name)
        current = self._require_active(project.project_id, "提交备案")

        equipment_counts = dict(declared_equipment or {})
        declared_staff = tuple(declared_personnel)
        issues = self._check_completeness(current, equipment_counts, declared_staff, declared_venue)
        filing = Filing(
            filing_id=self._next_id("filing", "FIL"),
            institution_id=institution,
            project_id=project.project_id,
            classification_version=current.version_no,
            snapshot=current,
            declared_equipment=equipment_counts,
            declared_personnel=declared_staff,
            declared_venue=declared_venue,
            state=FilingState.REGISTERED if issues else FilingState.PENDING,
            issues=issues,
            reviews=(),
            review_round=1,
            decision=None,
            seq=self._tick(),
        )
        self._filings[filing.filing_id] = filing
        return filing

    def update_declarations(
        self,
        filing_id: str,
        *,
        equipment: Mapping[str, int] | None = None,
        personnel: Iterable[DeclaredPersonnel] | None = None,
        venue: VenueRequirement | None | object = _UNSET,
    ) -> Filing:
        """补充申报材料；仅登记状态（尚未通过完整性检查或被退回）可修改。"""
        filing = self._get_filing(filing_id)
        if filing.state != FilingState.REGISTERED:
            raise InvalidStateError(f"当前状态「{filing.state}」不能修改申报材料")
        if equipment is not None:
            filing.declared_equipment = dict(equipment)
        if personnel is not None:
            filing.declared_personnel = tuple(personnel)
        if venue is not _UNSET:
            filing.declared_venue = venue  # type: ignore[assignment]
        return filing

    def resubmit_filing(self, filing_id: str, *, actor: str) -> Filing:
        """重新提交：按当前生效分类重跑完整性检查并重新钉住版本。"""
        self._require_actor(actor, (Role.COMPLIANCE,))
        filing = self._get_filing(filing_id)
        if filing.state != FilingState.REGISTERED:
            raise InvalidStateError(f"当前状态「{filing.state}」不能重新提交")
        current = self._require_active(filing.project_id, "重新提交备案")
        filing.classification_version = current.version_no
        filing.snapshot = current
        filing.issues = self._check_completeness(
            current, filing.declared_equipment, filing.declared_personnel, filing.declared_venue
        )
        if not filing.issues:
            filing.state = FilingState.PENDING
        return filing

    def review_filing(self, filing_id: str, *, role: str, outcome: str, note: str = "") -> Filing:
        """多角色复核：执业人员 → 复核专家 → 监管人员，顺序不可颠倒。

        通过/批准推进流程；退回回到登记状态并开启新一轮复核；
        监管人员批准或驳回后进入已决定，备案随之冻结。
        """
        filing = self._get_filing(filing_id)
        if filing.state not in (FilingState.PENDING, FilingState.REVIEWING):
            raise InvalidStateError(f"当前状态「{filing.state}」不能复核")
        if role not in REVIEW_SEQUENCE:
            raise DomainError(f"角色「{role}」不参与复核")
        self._require_active(filing.project_id, "继续复核")

        round_entries = [r for r in filing.reviews if r.round_no == filing.review_round]
        if len(round_entries) >= len(REVIEW_SEQUENCE):
            raise InvalidStateError("本轮复核已完成")
        expected = REVIEW_SEQUENCE[len(round_entries)]
        if role != expected:
            raise ReviewOrderError(f"复核顺序错误：应由「{expected}」复核，收到「{role}」")
        if outcome not in _OUTCOMES[role]:
            raise DomainError(f"「{role}」允许的结论：{'、'.join(_OUTCOMES[role])}")

        entry = Review(
            round_no=filing.review_round, role=role, outcome=outcome, note=note, seq=self._tick()
        )
        filing.reviews = filing.reviews + (entry,)
        if outcome == "退回":
            filing.state = FilingState.REGISTERED
            filing.review_round += 1
            filing.issues = filing.issues + (f"{role}退回：{note or '未说明原因'}",)
        elif role == Role.REGULATOR:
            filing.state = FilingState.DECIDED
            filing.decision = outcome
        else:
            filing.state = FilingState.REVIEWING
        return filing

    def archive_filing(self, filing_id: str, *, actor: str) -> Filing:
        """归档已决定的备案；归档后只读。"""
        self._require_actor(actor, (Role.REGULATOR,))
        filing = self._get_filing(filing_id)
        if filing.state != FilingState.DECIDED:
            raise InvalidStateError(f"当前状态「{filing.state}」不能归档")
        filing.state = FilingState.ARCHIVED
        return filing

    def get_filing(self, filing_id: str) -> Filing:
        return self._get_filing(filing_id)

    # ------------------------------------------------------------------
    # 历史服务记录
    # ------------------------------------------------------------------

    def record_service(self, filing_id: str, *, actor: str) -> ServiceRecord:
        """登记一次服务：钉住备案当时的分类版本，之后规则变更不影响该记录。"""
        self._require_actor(actor, (Role.PRACTITIONER, Role.COMPLIANCE))
        filing = self._get_filing(filing_id)
        if filing.decision != Decision.APPROVED or filing.state not in (
            FilingState.DECIDED,
            FilingState.ARCHIVED,
        ):
            raise InvalidStateError("仅已批准的备案可以登记服务")
        current = self.current_version(filing.project_id)
        if current.status != VersionStatus.ACTIVE:
            raise InvalidStateError(f"项目当前为「{current.status}」，不能登记新服务；历史服务记录不受影响")
        record = ServiceRecord(
            record_id=self._next_id("service", "SVC"),
            filing_id=filing.filing_id,
            institution_id=filing.institution_id,
            project_id=filing.project_id,
            classification_version=filing.classification_version,
            seq=self._tick(),
        )
        self._services[record.record_id] = record
        return record

    def service_history(
        self, *, institution_id: str | None = None, project_id: str | None = None
    ) -> list[ServiceRecord]:
        """查询历史服务记录，可按机构或项目过滤。"""
        records = [
            r
            for r in self._services.values()
            if (institution_id is None or r.institution_id == institution_id)
            and (project_id is None or r.project_id == project_id)
        ]
        return sorted(records, key=lambda r: r.seq)

    # ------------------------------------------------------------------
    # 差异比较与规则影响分析
    # ------------------------------------------------------------------

    def diff_versions(self, project_id: str, from_version: int, to_version: int) -> dict:
        """比较同一项目两个分类版本的差异。"""
        return diff_classification(
            self.get_version(project_id, from_version), self.get_version(project_id, to_version)
        )

    def impact_analysis(self, project_id: str, version_no: int | None = None) -> dict:
        """规则影响分析：列出受某版本（默认最新）影响的机构授权。

        - affected_authorizations：在旧版本下获批的机构授权，附与目标版本的差异；
        - pending_applications：仍钉在旧版本的在途申请；
        - pinned_service_records：历史服务记录，继续指向当时备案，不受变更影响。
        """
        target = self.get_version(project_id, version_no) if version_no is not None else self.current_version(project_id)
        filings = sorted(
            (f for f in self._filings.values() if f.project_id == project_id),
            key=lambda f: f.filing_id,
        )
        affected = [
            {
                "filing_id": f.filing_id,
                "institution_id": f.institution_id,
                "authorized_version": f.classification_version,
                "changes": diff_classification(f.snapshot, target),
            }
            for f in filings
            if f.decision == Decision.APPROVED and f.classification_version < target.version_no
        ]
        pending = [
            {
                "filing_id": f.filing_id,
                "institution_id": f.institution_id,
                "state": f.state,
                "pinned_version": f.classification_version,
            }
            for f in filings
            if f.decision is None and f.classification_version < target.version_no
        ]
        services = sorted(
            (r for r in self._services.values() if r.project_id == project_id), key=lambda r: r.seq
        )
        return {
            "project_id": project_id,
            "version_no": target.version_no,
            "change_type": target.change_type,
            "status": target.status,
            "affected_authorizations": affected,
            "pending_applications": pending,
            "pinned_service_records": [
                {
                    "record_id": r.record_id,
                    "filing_id": r.filing_id,
                    "institution_id": r.institution_id,
                    "classification_version": r.classification_version,
                }
                for r in services
            ],
        }
