"""医美项目分级备案后端的行为测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from domain_contract.validator import load_contract
from procedure_filing import (
    ChangeType,
    Decision,
    DeclaredPersonnel,
    DomainError,
    EquipmentRequirement,
    FilingService,
    FilingState,
    InvalidStateError,
    PersonnelRequirement,
    ReviewOrderError,
    Role,
    UnknownProjectError,
    VenueRequirement,
    VersionStatus,
)

VENUE = VenueRequirement("治疗室", 15.0)
STAFF_REQUIREMENTS = (
    PersonnelRequirement("主诊医师", "美容主诊医师备案"),
    PersonnelRequirement("护士", "护士执业证书"),
)
DEVICE = EquipmentRequirement("强脉冲光治疗仪")


def declared_staff() -> tuple[DeclaredPersonnel, ...]:
    return (
        DeclaredPersonnel("主诊医师", "美容主诊医师备案"),
        DeclaredPersonnel("护士", "护士执业证书"),
    )


def make_service() -> FilingService:
    """两个名称相近、适用条件完全不同的项目。"""
    svc = FilingService()
    svc.register_project(
        "光子嫩肤",
        aliases=["强脉冲光嫩肤"],
        risk_level="一级",
        venue=VENUE,
        personnel=STAFF_REQUIREMENTS,
        equipment=[DEVICE],
        actor=Role.REGULATOR,
    )
    svc.register_project(
        "光子嫩肤术",
        risk_level="三级",
        venue=VenueRequirement("手术室", 30.0),
        personnel=(
            PersonnelRequirement("主诊医师", "美容主诊医师备案"),
            PersonnelRequirement("麻醉医师", "麻醉科执业范围"),
        ),
        equipment=[EquipmentRequirement("麻醉机"), EquipmentRequirement("心电监护仪")],
        actor=Role.REGULATOR,
    )
    return svc


def complete_filing(svc: FilingService, institution: str, project: str = "光子嫩肤"):
    return svc.submit_filing(
        institution,
        project_name=project,
        actor=Role.COMPLIANCE,
        declared_equipment={"强脉冲光治疗仪": 1},
        declared_personnel=declared_staff(),
        declared_venue=VenueRequirement("治疗室", 20.0),
    )


def approve_all(svc: FilingService, filing_id: str) -> None:
    svc.review_filing(filing_id, role=Role.PRACTITIONER, outcome="通过", note="执业条件符合")
    svc.review_filing(filing_id, role=Role.EXPERT, outcome="通过", note="分类适用正确")
    svc.review_filing(filing_id, role=Role.REGULATOR, outcome=Decision.APPROVED, note="同意备案")


class CatalogTest(unittest.TestCase):
    def test_resolve_by_name_and_alias(self) -> None:
        svc = make_service()
        by_name = svc.resolve_project("光子嫩肤")
        by_alias = svc.resolve_project("强脉冲光嫩肤")
        self.assertEqual(by_name.project_id, by_alias.project_id)

    def test_similar_names_are_distinct_projects(self) -> None:
        svc = make_service()
        light = svc.current_classification("光子嫩肤")
        surgery = svc.current_classification("光子嫩肤术")
        self.assertNotEqual(light.project_id, surgery.project_id)
        self.assertEqual(light.risk_level, "一级")
        self.assertEqual(surgery.risk_level, "三级")
        self.assertEqual(surgery.venue.room_type, "手术室")

    def test_find_similar_lists_nearby_projects(self) -> None:
        svc = make_service()
        names = {p.name for p in svc.find_similar("光子嫩肤")}
        self.assertEqual(names, {"光子嫩肤", "光子嫩肤术"})
        self.assertEqual(svc.find_similar(""), [])

    def test_duplicate_name_or_alias_rejected(self) -> None:
        svc = make_service()
        with self.assertRaises(DomainError):
            svc.register_project("光子嫩肤", risk_level="一级", actor=Role.REGULATOR)
        with self.assertRaises(DomainError):
            svc.register_project("新项目名称", aliases=["强脉冲光嫩肤"], risk_level="一级", actor=Role.REGULATOR)
        with self.assertRaises(DomainError):
            svc.add_alias(svc.resolve_project("光子嫩肤术").project_id, "光子嫩肤", actor=Role.EXPERT)

    def test_add_alias_and_resolve(self) -> None:
        svc = make_service()
        pid = svc.resolve_project("光子嫩肤").project_id
        svc.add_alias(pid, "IPL嫩肤", actor=Role.EXPERT)
        self.assertEqual(svc.resolve_project("IPL嫩肤").project_id, pid)

    def test_unknown_name_raises(self) -> None:
        svc = make_service()
        with self.assertRaises(UnknownProjectError):
            svc.resolve_project("超声刀")

    def test_current_classification_reports_requirements(self) -> None:
        svc = make_service()
        current = svc.current_classification("强脉冲光嫩肤")
        self.assertEqual(current.risk_level, "一级")
        self.assertEqual(current.venue, VENUE)
        self.assertEqual(current.personnel, STAFF_REQUIREMENTS)
        self.assertEqual(current.equipment, (DEVICE,))

    def test_catalog_requires_privileged_role(self) -> None:
        svc = make_service()
        with self.assertRaises(DomainError):
            svc.register_project("水光注射", risk_level="二级", actor=Role.COMPLIANCE)


class CompletenessTest(unittest.TestCase):
    def test_missing_dependencies_reported_and_stay_registered(self) -> None:
        svc = make_service()
        filing = svc.submit_filing(
            "INST-A",
            project_name="光子嫩肤",
            actor=Role.COMPLIANCE,
            declared_equipment={},
            declared_personnel=[DeclaredPersonnel("主诊医师", "美容主诊医师备案")],
            declared_venue=VenueRequirement("治疗室", 10.0),
        )
        self.assertEqual(filing.state, FilingState.REGISTERED)
        text = "\n".join(filing.issues)
        self.assertIn("强脉冲光治疗仪", text)
        self.assertIn("护士", text)
        self.assertIn("面积不足", text)

    def test_wrong_venue_type_reported(self) -> None:
        svc = make_service()
        filing = svc.submit_filing(
            "INST-A",
            project_name="光子嫩肤",
            actor=Role.COMPLIANCE,
            declared_equipment={"强脉冲光治疗仪": 1},
            declared_personnel=declared_staff(),
            declared_venue=VenueRequirement("手术室", 20.0),
        )
        self.assertTrue(any("场地类型不符" in i for i in filing.issues))

    def test_complete_declaration_enters_review(self) -> None:
        svc = make_service()
        filing = complete_filing(svc, "INST-A")
        self.assertEqual(filing.state, FilingState.PENDING)
        self.assertEqual(filing.issues, ())

    def test_update_and_resubmit(self) -> None:
        svc = make_service()
        filing = svc.submit_filing("INST-A", project_name="光子嫩肤", actor=Role.COMPLIANCE)
        self.assertEqual(filing.state, FilingState.REGISTERED)
        svc.update_declarations(
            filing.filing_id,
            equipment={"强脉冲光治疗仪": 1},
            personnel=declared_staff(),
            venue=VenueRequirement("治疗室", 18.0),
        )
        again = svc.resubmit_filing(filing.filing_id, actor=Role.COMPLIANCE)
        self.assertEqual(again.state, FilingState.PENDING)
        self.assertEqual(again.issues, ())

    def test_resubmit_still_incomplete_stays_registered(self) -> None:
        svc = make_service()
        filing = svc.submit_filing("INST-A", project_name="光子嫩肤", actor=Role.COMPLIANCE)
        svc.update_declarations(filing.filing_id, equipment={"强脉冲光治疗仪": 1})
        again = svc.resubmit_filing(filing.filing_id, actor=Role.COMPLIANCE)
        self.assertEqual(again.state, FilingState.REGISTERED)
        self.assertTrue(any("护士" in i for i in again.issues))


class ReviewFlowTest(unittest.TestCase):
    def test_review_order_enforced(self) -> None:
        svc = make_service()
        filing = complete_filing(svc, "INST-A")
        with self.assertRaises(ReviewOrderError):
            svc.review_filing(filing.filing_id, role=Role.EXPERT, outcome="通过")
        with self.assertRaises(ReviewOrderError):
            svc.review_filing(filing.filing_id, role=Role.REGULATOR, outcome=Decision.APPROVED)

    def test_full_review_approves_then_archives(self) -> None:
        svc = make_service()
        filing = complete_filing(svc, "INST-A")
        svc.review_filing(filing.filing_id, role=Role.PRACTITIONER, outcome="通过")
        self.assertEqual(filing.state, FilingState.REVIEWING)
        svc.review_filing(filing.filing_id, role=Role.EXPERT, outcome="通过")
        svc.review_filing(filing.filing_id, role=Role.REGULATOR, outcome=Decision.APPROVED)
        self.assertEqual(filing.state, FilingState.DECIDED)
        self.assertEqual(filing.decision, Decision.APPROVED)
        svc.archive_filing(filing.filing_id, actor=Role.REGULATOR)
        self.assertEqual(filing.state, FilingState.ARCHIVED)
        with self.assertRaises(InvalidStateError):
            svc.review_filing(filing.filing_id, role=Role.PRACTITIONER, outcome="通过")

    def test_return_reopens_registration_and_new_round(self) -> None:
        svc = make_service()
        filing = complete_filing(svc, "INST-A")
        svc.review_filing(filing.filing_id, role=Role.PRACTITIONER, outcome="通过")
        svc.review_filing(filing.filing_id, role=Role.EXPERT, outcome="退回", note="人员资质存疑")
        self.assertEqual(filing.state, FilingState.REGISTERED)
        self.assertTrue(any("人员资质存疑" in i for i in filing.issues))
        svc.resubmit_filing(filing.filing_id, actor=Role.COMPLIANCE)
        # 新一轮复核从头开始
        svc.review_filing(filing.filing_id, role=Role.PRACTITIONER, outcome="通过")
        svc.review_filing(filing.filing_id, role=Role.EXPERT, outcome="通过")
        svc.review_filing(filing.filing_id, role=Role.REGULATOR, outcome=Decision.APPROVED)
        self.assertEqual(filing.decision, Decision.APPROVED)
        rounds = {r.round_no for r in filing.reviews}
        self.assertEqual(rounds, {1, 2})

    def test_regulator_rejection_is_final(self) -> None:
        svc = make_service()
        filing = complete_filing(svc, "INST-A")
        svc.review_filing(filing.filing_id, role=Role.PRACTITIONER, outcome="通过")
        svc.review_filing(filing.filing_id, role=Role.EXPERT, outcome="通过")
        svc.review_filing(filing.filing_id, role=Role.REGULATOR, outcome=Decision.REJECTED, note="条件不具备")
        self.assertEqual(filing.state, FilingState.DECIDED)
        self.assertEqual(filing.decision, Decision.REJECTED)
        with self.assertRaises(InvalidStateError):
            svc.record_service(filing.filing_id, actor=Role.PRACTITIONER)

    def test_non_review_role_rejected(self) -> None:
        svc = make_service()
        filing = complete_filing(svc, "INST-A")
        with self.assertRaises(DomainError):
            svc.review_filing(filing.filing_id, role=Role.COMPLIANCE, outcome="通过")


class VersionChainTest(unittest.TestCase):
    def test_correction_appends_version(self) -> None:
        svc = make_service()
        pid = svc.resolve_project("光子嫩肤").project_id
        v2 = svc.correct_classification(
            pid, actor=Role.EXPERT, reason="不良事件率上调", risk_level="二级"
        )
        self.assertEqual(v2.version_no, 2)
        self.assertEqual(v2.parent_version, 1)
        self.assertEqual(v2.change_type, ChangeType.CORRECTION)
        chain = svc.version_chain(pid)
        self.assertEqual([v.version_no for v in chain], [1, 2])
        self.assertEqual(chain[0].risk_level, "一级")
        self.assertEqual(svc.current_version(pid).risk_level, "二级")

    def test_correction_requires_reason(self) -> None:
        svc = make_service()
        pid = svc.resolve_project("光子嫩肤").project_id
        with self.assertRaises(DomainError):
            svc.correct_classification(pid, actor=Role.EXPERT, reason="", risk_level="二级")

    def test_substitution_replaces_dependency(self) -> None:
        svc = make_service()
        pid = svc.resolve_project("光子嫩肤").project_id
        v2 = svc.substitute_dependency(
            pid,
            actor=Role.REGULATOR,
            reason="设备换代",
            remove_equipment=["强脉冲光治疗仪"],
            add_equipment=[EquipmentRequirement("强脉冲光治疗仪Pro")],
        )
        self.assertEqual(v2.change_type, ChangeType.SUBSTITUTION)
        self.assertEqual([e.equipment for e in v2.equipment], ["强脉冲光治疗仪Pro"])
        # 新版本下可以用替代设备通过完整性检查
        filing = svc.submit_filing(
            "INST-A",
            project_name="光子嫩肤",
            actor=Role.COMPLIANCE,
            declared_equipment={"强脉冲光治疗仪Pro": 1},
            declared_personnel=declared_staff(),
            declared_venue=VenueRequirement("治疗室", 20.0),
        )
        self.assertEqual(filing.state, FilingState.PENDING)

    def test_substitution_rejects_unknown_removal(self) -> None:
        svc = make_service()
        pid = svc.resolve_project("光子嫩肤").project_id
        with self.assertRaises(DomainError):
            svc.substitute_dependency(
                pid, actor=Role.REGULATOR, reason="误操作", remove_equipment=["超声刀"]
            )

    def test_emergency_suspend_blocks_new_activity(self) -> None:
        svc = make_service()
        pid = svc.resolve_project("光子嫩肤术").project_id
        approved = svc.submit_filing(
            "INST-A",
            project_name="光子嫩肤术",
            actor=Role.COMPLIANCE,
            declared_equipment={"麻醉机": 1, "心电监护仪": 1},
            declared_personnel=(
                DeclaredPersonnel("主诊医师", "美容主诊医师备案"),
                DeclaredPersonnel("麻醉医师", "麻醉科执业范围"),
            ),
            declared_venue=VenueRequirement("手术室", 40.0),
        )
        approve_all(svc, approved.filing_id)
        in_flight = svc.submit_filing(
            "INST-B",
            project_name="光子嫩肤术",
            actor=Role.COMPLIANCE,
            declared_equipment={"麻醉机": 1, "心电监护仪": 1},
            declared_personnel=(
                DeclaredPersonnel("主诊医师", "美容主诊医师备案"),
                DeclaredPersonnel("麻醉医师", "麻醉科执业范围"),
            ),
            declared_venue=VenueRequirement("手术室", 40.0),
        )
        v2 = svc.emergency_suspend(pid, actor=Role.REGULATOR, reason="严重不良事件调查")
        self.assertEqual(v2.status, VersionStatus.SUSPENDED)
        self.assertEqual(v2.change_type, ChangeType.SUSPENSION)
        with self.assertRaises(InvalidStateError):
            svc.submit_filing("INST-C", project_name="光子嫩肤术", actor=Role.COMPLIANCE)
        with self.assertRaises(InvalidStateError):
            svc.review_filing(in_flight.filing_id, role=Role.PRACTITIONER, outcome="通过")
        with self.assertRaises(InvalidStateError):
            svc.record_service(approved.filing_id, actor=Role.PRACTITIONER)

    def test_suspend_requires_regulator(self) -> None:
        svc = make_service()
        pid = svc.resolve_project("光子嫩肤").project_id
        with self.assertRaises(DomainError):
            svc.emergency_suspend(pid, actor=Role.EXPERT, reason="越权尝试")

    def test_split_retires_original_and_creates_children(self) -> None:
        svc = make_service()
        svc.register_project(
            "水光注射",
            risk_level="二级",
            venue=VENUE,
            personnel=STAFF_REQUIREMENTS,
            equipment=[EquipmentRequirement("注射泵")],
            actor=Role.REGULATOR,
        )
        pid = svc.resolve_project("水光注射").project_id
        children = svc.split_project(
            pid,
            actor=Role.EXPERT,
            reason="技术路径分化",
            splits=[
                {
                    "name": "水光注射（透明质酸）",
                    "aliases": ["透明质酸水光"],
                    "risk_level": "二级",
                    "venue": VENUE,
                    "personnel": STAFF_REQUIREMENTS,
                    "equipment": [EquipmentRequirement("注射泵")],
                },
                {
                    "name": "水光注射（胶原蛋白）",
                    "risk_level": "三级",
                    "venue": VenueRequirement("手术室", 25.0),
                    "personnel": STAFF_REQUIREMENTS,
                    "equipment": [EquipmentRequirement("注射泵"), EquipmentRequirement("心电监护仪")],
                },
            ],
        )
        self.assertEqual(len(children), 2)
        original = svc.current_version(pid)
        self.assertEqual(original.status, VersionStatus.SPLIT)
        self.assertEqual(original.change_type, ChangeType.SPLIT)
        self.assertEqual(svc.get_project(pid).split_into, tuple(c.project_id for c in children))
        for child in children:
            first = svc.get_version(child.project_id, 1)
            self.assertEqual(first.change_type, ChangeType.SPLIT)
            self.assertEqual(first.source_project_id, pid)
        with self.assertRaises(InvalidStateError):
            svc.submit_filing("INST-A", project_name="水光注射", actor=Role.COMPLIANCE)
        # 子项目可以正常备案
        filing = svc.submit_filing(
            "INST-A",
            project_name="透明质酸水光",
            actor=Role.COMPLIANCE,
            declared_equipment={"注射泵": 1},
            declared_personnel=declared_staff(),
            declared_venue=VenueRequirement("治疗室", 20.0),
        )
        self.assertEqual(filing.state, FilingState.PENDING)

    def test_split_requires_two_distinct_children(self) -> None:
        svc = make_service()
        pid = svc.resolve_project("光子嫩肤").project_id
        with self.assertRaises(DomainError):
            svc.split_project(
                pid,
                actor=Role.EXPERT,
                reason="拆分",
                splits=[{"name": "唯一子项目", "risk_level": "一级"}],
            )
        with self.assertRaises(DomainError):
            svc.split_project(
                pid,
                actor=Role.EXPERT,
                reason="拆分",
                splits=[
                    {"name": "同名子项目", "risk_level": "一级"},
                    {"name": "同名子项目", "risk_level": "二级"},
                ],
            )


class HistoryFreezeTest(unittest.TestCase):
    def test_service_records_stay_pinned_to_filing_version(self) -> None:
        svc = make_service()
        pid = svc.resolve_project("光子嫩肤").project_id
        filing = complete_filing(svc, "INST-A")
        approve_all(svc, filing.filing_id)
        record = svc.record_service(filing.filing_id, actor=Role.PRACTITIONER)
        self.assertEqual(record.classification_version, 1)

        svc.correct_classification(pid, actor=Role.EXPERT, reason="风险重估", risk_level="二级")
        history = svc.service_history(institution_id="INST-A")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].classification_version, 1)
        self.assertEqual(history[0].filing_id, filing.filing_id)

    def test_decided_filing_snapshot_frozen_after_correction(self) -> None:
        svc = make_service()
        pid = svc.resolve_project("光子嫩肤").project_id
        filing = complete_filing(svc, "INST-A")
        approve_all(svc, filing.filing_id)
        svc.correct_classification(pid, actor=Role.EXPERT, reason="风险重估", risk_level="四级")
        self.assertEqual(filing.snapshot.risk_level, "一级")
        self.assertEqual(filing.classification_version, 1)
        self.assertEqual(svc.current_version(pid).risk_level, "四级")


class AnalysisTest(unittest.TestCase):
    def _scenario(self) -> tuple[FilingService, str]:
        """v1 批准 INST-A → v2 分类更正 → INST-B 获批、INST-C 在途 → v3 依赖替代。"""
        svc = make_service()
        pid = svc.resolve_project("光子嫩肤").project_id

        fa = complete_filing(svc, "INST-A")
        approve_all(svc, fa.filing_id)
        svc.record_service(fa.filing_id, actor=Role.PRACTITIONER)

        svc.correct_classification(
            pid,
            actor=Role.EXPERT,
            reason="不良事件率上调",
            risk_level="二级",
            equipment=[DEVICE, EquipmentRequirement("皮肤检测仪")],
        )
        fb = svc.submit_filing(
            "INST-B",
            project_name="光子嫩肤",
            actor=Role.COMPLIANCE,
            declared_equipment={"强脉冲光治疗仪": 1, "皮肤检测仪": 1},
            declared_personnel=declared_staff(),
            declared_venue=VenueRequirement("治疗室", 20.0),
        )
        approve_all(svc, fb.filing_id)
        svc.submit_filing(
            "INST-C",
            project_name="光子嫩肤",
            actor=Role.COMPLIANCE,
            declared_equipment={"强脉冲光治疗仪": 1, "皮肤检测仪": 1},
            declared_personnel=declared_staff(),
            declared_venue=VenueRequirement("治疗室", 20.0),
        )
        svc.substitute_dependency(
            pid,
            actor=Role.REGULATOR,
            reason="设备换代",
            remove_equipment=["强脉冲光治疗仪"],
            add_equipment=[EquipmentRequirement("强脉冲光治疗仪Pro")],
        )
        return svc, pid

    def test_diff_reports_risk_and_dependency_changes(self) -> None:
        svc, pid = self._scenario()
        diff = svc.diff_versions(pid, 1, 3)
        self.assertEqual(diff["risk_level"], {"from": "一级", "to": "二级", "direction": "上调"})
        added = {e["equipment"] for e in diff["equipment_added"]}
        removed = {e["equipment"] for e in diff["equipment_removed"]}
        self.assertEqual(added, {"皮肤检测仪", "强脉冲光治疗仪Pro"})
        self.assertEqual(removed, {"强脉冲光治疗仪"})
        self.assertFalse(diff["venue"]["changed"])
        self.assertEqual(diff["personnel_added"], [])
        self.assertEqual(diff["personnel_removed"], [])

    def test_impact_lists_affected_authorizations(self) -> None:
        svc, pid = self._scenario()
        impact = svc.impact_analysis(pid)
        self.assertEqual(impact["version_no"], 3)
        self.assertEqual(impact["change_type"], ChangeType.SUBSTITUTION)
        affected = {a["institution_id"]: a for a in impact["affected_authorizations"]}
        self.assertEqual(set(affected), {"INST-A", "INST-B"})
        self.assertEqual(affected["INST-A"]["authorized_version"], 1)
        self.assertEqual(affected["INST-B"]["authorized_version"], 2)
        self.assertEqual(affected["INST-A"]["changes"]["risk_level"]["direction"], "上调")
        self.assertEqual(affected["INST-B"]["changes"]["risk_level"]["direction"], "不变")

    def test_impact_lists_pending_and_pinned_services(self) -> None:
        svc, pid = self._scenario()
        impact = svc.impact_analysis(pid)
        pending = {p["institution_id"] for p in impact["pending_applications"]}
        self.assertEqual(pending, {"INST-C"})
        pinned = impact["pinned_service_records"]
        self.assertEqual(len(pinned), 1)
        self.assertEqual(pinned[0]["classification_version"], 1)

    def test_impact_of_older_version_ignores_later_filings(self) -> None:
        svc, pid = self._scenario()
        impact = svc.impact_analysis(pid, version_no=2)
        affected = {a["institution_id"] for a in impact["affected_authorizations"]}
        self.assertEqual(affected, {"INST-A"})


class ContractAlignmentTest(unittest.TestCase):
    def test_roles_and_states_match_contract(self) -> None:
        contract = load_contract(ROOT / "domain" / "contract.json")
        self.assertEqual(set(Role.ALL), set(contract["actors"]))
        self.assertEqual(set(FilingState.ALL), set(contract["states"]))


if __name__ == "__main__":
    unittest.main()
