"""医美分级备案后端的领域回归测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medical_filing import FilingService
from medical_filing.api import make_server
from medical_filing.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
    WorkflowError,
)


class BackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = FilingService(":memory:", seed=True)

    def tearDown(self) -> None:
        self.svc.close()

    # ---------------------------------------------------------- 目录与别名
    def test_alias_resolves_to_distinct_similar_named_procedure(self) -> None:
        direct = self.svc.resolve_procedure("P001")
        via_alias = self.svc.resolve_procedure("抽脂术")
        via_name = self.svc.resolve_procedure("脂肪重塑术（射频溶脂）")
        self.assertEqual(direct["code"], "P001")
        self.assertEqual(via_alias["code"], "P001")
        # 名称相近的两个项目分级不同，不能混淆
        self.assertEqual(via_name["code"], "P002")
        self.assertNotEqual(via_alias["current_rule"]["risk_level"], via_name["current_rule"]["risk_level"])
        with self.assertRaises(NotFoundError):
            self.svc.resolve_procedure("溶脂")

    def test_alias_cannot_be_shared(self) -> None:
        with self.assertRaises(ConflictError):
            self.svc.add_alias("P003", "抽脂术")

    # ---------------------------------------------------------- 完整性检查
    def test_completeness_check_pass_and_fail(self) -> None:
        ok = self.svc.run_completeness_check("F001", "P001")
        self.assertTrue(ok["passed"])
        bad = self.svc.run_completeness_check("F002", "P001")
        self.assertFalse(bad["passed"])
        self.assertIn("普通手术室", bad["gaps"]["site_requirements"])
        self.assertIn("吸脂机", bad["gaps"]["required_equipment"])
        self.assertIn("麻醉医师", bad["gaps"]["required_personnel"])

    def test_submit_blocked_until_check_passes(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.svc.submit_filing("F002", "P001")
        self.assertIsNotNone(ctx.exception.details)
        self.assertFalse(ctx.exception.details["passed"])

    # ---------------------------------------------------------- 多角色复核
    def _authorize(self, facility: str = "F001", procedure: str = "P001") -> str:
        filing = self.svc.submit_filing(facility, procedure)
        for role in ("执业人员", "复核专家", "监管人员"):
            self.svc.review_filing(filing["id"], role, reviewer=role + "甲", approve=True)
        return filing["id"]

    def test_review_must_follow_role_sequence(self) -> None:
        filing = self.svc.submit_filing("F001", "P001")
        with self.assertRaises(WorkflowError):
            self.svc.review_filing(filing["id"], "监管人员", reviewer="监管", approve=True)
        self.svc.review_filing(filing["id"], "执业人员", reviewer="技术", approve=True)
        with self.assertRaises(WorkflowError):
            self.svc.review_filing(filing["id"], "监管人员", reviewer="监管", approve=True)
        self.svc.review_filing(filing["id"], "复核专家", reviewer="专家", approve=True)
        final = self.svc.review_filing(filing["id"], "监管人员", reviewer="监管", approve=True)
        self.assertEqual(final["status"], "已授权")
        self.assertEqual([t["status"] for t in final["review_tasks"]], ["done", "done", "done"])

    def test_rejection_terminates_review_chain(self) -> None:
        filing = self.svc.submit_filing("F001", "P001")
        self.svc.review_filing(filing["id"], "执业人员", reviewer="技术", approve=True)
        result = self.svc.review_filing(filing["id"], "复核专家", reviewer="专家", approve=False, comment="存疑")
        self.assertEqual(result["status"], "已驳回")
        task_status = {t["role"]: t["status"] for t in result["review_tasks"]}
        self.assertEqual(task_status["监管人员"], "skipped")
        with self.assertRaises(WorkflowError):
            self.svc.review_filing(filing["id"], "监管人员", reviewer="监管", approve=True)

    # ---------------------------------------------------------- 规则版本链
    def test_correction_builds_version_chain_and_diff(self) -> None:
        v1 = self.svc.get_procedure("P002")["current_rule"]
        v2 = self.svc.correct_classification(
            "P002",
            risk_level="三级",
            site_requirements=["普通手术室", "独立消毒间"],
            required_equipment=["射频溶脂设备", "监护仪"],
            required_personnel=["主诊医师", "麻醉医师", "护士"],
            reason="分级目录调整",
        )
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["change_type"], "correction")
        self.assertEqual(v2["replaces_rule_id"], v1["id"])
        versions = self.svc.list_rule_versions("P002")
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(versions[0]["superseded_by_rule_id"], v2["id"])

        diff = self.svc.diff_rules(v1["id"], v2["id"])
        self.assertTrue(diff["risk_level"]["changed"])
        self.assertEqual(diff["risk_level"]["from"], "二级")
        self.assertEqual(diff["risk_level"]["to"], "三级")
        self.assertIn("监护仪", diff["required_equipment"]["added"])
        self.assertIn("麻醉医师", diff["required_personnel"]["added"])

    def test_substitution_replaces_dependency_only(self) -> None:
        rule = self.svc.substitute_dependencies(
            "P001",
            [{"kind": "equipment", "from": "吸脂机", "to": "新一代水动力吸脂系统"}],
            reason="设备更新换代",
        )
        self.assertEqual(rule["change_type"], "substitution")
        self.assertNotIn("吸脂机", rule["required_equipment"])
        self.assertIn("新一代水动力吸脂系统", rule["required_equipment"])
        self.assertEqual(rule["substitutions"][0]["from"], "吸脂机")
        with self.assertRaises(ValidationError):
            self.svc.substitute_dependencies("P001", [{"kind": "equipment", "from": "不存在", "to": "x"}], "r")

    def test_split_derives_new_procedures(self) -> None:
        result = self.svc.split_procedure(
            "P002",
            [
                {
                    "code": "P002A",
                    "name": "射频溶脂（躯体）",
                    "risk_level": "三级",
                    "site_requirements": ["普通手术室"],
                    "required_equipment": ["射频溶脂设备", "监护仪"],
                    "required_personnel": ["主诊医师", "麻醉医师"],
                    "aliases": ["身体射频溶脂"],
                },
                {
                    "code": "P002B",
                    "name": "射频溶脂（面部）",
                    "risk_level": "二级",
                    "site_requirements": ["独立治疗室"],
                    "required_equipment": ["射频溶脂设备"],
                    "required_personnel": ["主诊医师"],
                },
            ],
            reason="按部位拆分管理",
        )
        self.assertEqual(result["split_rule"]["change_type"], "split")
        self.assertEqual(self.svc.get_procedure("P002")["status"], "split")
        derivative = self.svc.get_procedure("P002A")["current_rule"]
        self.assertEqual(derivative["change_type"], "split_derivative")
        self.assertEqual(derivative["replaces_rule_id"], "R-P002-V1")
        with self.assertRaises(ValidationError):
            self.svc.submit_filing("F001", "P002")
        self.assertEqual(self.svc.resolve_procedure("身体射频溶脂")["code"], "P002A")

    def test_emergency_suspension_blocks_new_filings_but_keeps_history(self) -> None:
        filing_id = self._authorize()
        svc_record = self.svc.register_service(filing_id, note="暂停前完成的服务")
        self.svc.emergency_suspend("P001", reason="不良事件调查")
        with self.assertRaises(ValidationError):
            self.svc.submit_filing("F001", "P001")
        # 历史服务仍指向备案当时的 V1 规则
        history = self.svc.get_service(svc_record["id"])
        self.assertEqual(history["filing"]["frozen_rule_id"], "R-P001-V1")
        # 更正后恢复
        self.svc.correct_classification(
            "P001",
            risk_level="三级",
            site_requirements=["普通手术室", "独立消毒间", "留观室"],
            required_equipment=["监护仪", "吸脂机", "电凝止血设备"],
            required_personnel=["主诊医师", "麻醉医师", "护士"],
            reason="调查结论：维持三级并补充要求",
        )
        self.assertEqual(self.svc.get_procedure("P001")["status"], "active")

    # ---------------------------------------------------------- 历史冻结与影响
    def test_history_keeps_frozen_rule_after_correction(self) -> None:
        filing_id = self._authorize()
        before = self.svc.register_service(filing_id, note="旧规则下服务")
        self.svc.correct_classification(
            "P001",
            risk_level="四级",
            site_requirements=["洁净手术室", "独立消毒间", "留观室", "抢救室"],
            required_equipment=["监护仪", "吸脂机", "电凝止血设备", "除颤仪"],
            required_personnel=["主诊医师", "麻醉医师", "护士"],
            reason="风险上调",
        )
        history = self.svc.get_service(before["id"])
        self.assertEqual(history["filing"]["frozen_risk_level"], "三级")
        filing = self.svc.get_filing(filing_id)
        self.assertEqual(filing["frozen_rule"]["version"], 1)

    def test_impact_analysis_lists_affected_authorizations(self) -> None:
        fid = self._authorize()
        # 规则收紧：F001 缺除颤仪与抢救室
        self.svc.correct_classification(
            "P001",
            risk_level="三级",
            site_requirements=["普通手术室", "独立消毒间", "留观室", "抢救室"],
            required_equipment=["监护仪", "吸脂机", "电凝止血设备", "除颤仪"],
            required_personnel=["主诊医师", "麻醉医师", "护士"],
            reason="新增抢救条件",
        )
        impact = self.svc.analyze_impact("P001")
        self.assertEqual(impact["affected_authorization_count"], 1)
        item = impact["authorizations"][0]
        self.assertEqual(item["filing_id"], fid)
        self.assertFalse(item["satisfies_latest"])
        self.assertIn("抢救室", item["missing"]["site_requirements"])
        self.assertIn("除颤仪", item["missing"]["required_equipment"])

        # 机构补齐资源后重新分析即不再受影响
        facility = self.svc.get_facility("F001")
        self.svc.update_facility_resources(
            "F001",
            site_capability=facility["site_capability"] + ["抢救室"],
            equipment=facility["equipment"] + ["除颤仪"],
        )
        self.assertEqual(self.svc.analyze_impact("P001")["affected_authorization_count"], 0)

    def test_suspension_flags_all_authorizations_in_impact(self) -> None:
        self._authorize()
        self.svc.emergency_suspend("P001", reason="调查")
        impact = self.svc.analyze_impact("P001")
        self.assertEqual(impact["affected_authorization_count"], 1)
        self.assertIn("紧急暂停", impact["authorizations"][0]["reasons"][0])

    def test_split_flags_existing_authorizations_in_impact(self) -> None:
        # F001 满足 P003（一级红蓝光）：为其取得授权后拆分 P003
        self.svc.update_facility_resources(
            "F002",
            site_capability=["独立治疗室"],
            staff=[{"role": "主诊医师", "count": 1}, {"role": "护士", "count": 1}],
            equipment=["红蓝光治疗仪"],
        )
        fid = self._authorize(facility="F002", procedure="P003")
        self.svc.split_procedure(
            "P003",
            [
                {
                    "code": "P003A",
                    "name": "红光修复治疗",
                    "risk_level": "一级",
                    "site_requirements": ["独立治疗室"],
                    "required_equipment": ["红光治疗仪"],
                    "required_personnel": ["主诊医师"],
                }
            ],
            reason="按光类拆分",
        )
        impact = self.svc.analyze_impact("P003")
        self.assertEqual(impact["authorizations"][0]["filing_id"], fid)
        self.assertIn("项目已拆分", impact["authorizations"][0]["reasons"][0])
        self.assertFalse(impact["authorizations"][0]["satisfies_latest"])

    def test_archived_filing_remains_traceable(self) -> None:
        fid = self._authorize()
        self.svc.archive_filing(fid)
        record = self.svc.register_service(fid, note="归档备案的追溯服务登记")
        self.assertEqual(self.svc.get_service(record["id"])["filing"]["status"], "已归档")


class ApiTest(unittest.TestCase):
    """HTTP 层冒烟：路由、JSON 错误体、PATCH 与跨线程访问。"""

    def setUp(self) -> None:
        self.server = make_server("127.0.0.1", 0, ":memory:", seed=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.RequestHandlerClass.service.close()
        self.server.server_close()

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_check_failure_returns_json_400(self) -> None:
        status, body = self._request(
            "POST", "/api/completeness-checks", {"facility_id": "F002", "procedure_ref": "P001"}
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["passed"])

    def test_submit_blocked_is_400_with_details(self) -> None:
        status, body = self._request("POST", "/api/filings", {"facility_id": "F002", "procedure_ref": "P001"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "validation_error")
        self.assertFalse(body["details"]["passed"])

    def test_out_of_order_review_is_409_and_patch_works(self) -> None:
        _, filing = self._request("POST", "/api/filings", {"facility_id": "F001", "procedure_ref": "P001"})
        status, body = self._request(
            "POST",
            f"/api/filings/{filing['id']}/reviews",
            {"role": "监管人员", "reviewer": "监管", "approve": True},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "workflow_error")

        status, body = self._request(
            "PATCH",
            "/api/facilities/F002",
            {"equipment": ["红蓝光治疗仪", "射频溶脂设备"]},
        )
        self.assertEqual(status, 200)
        self.assertIn("射频溶脂设备", body["equipment"])
        # 未提交的字段保持不变
        self.assertEqual(body["site_capability"], ["独立治疗室"])


if __name__ == "__main__":
    unittest.main()
