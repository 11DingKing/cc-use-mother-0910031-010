"""备案申请：完整性检查、多角色有序复核、历史冻结与历史服务。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .catalog import get_facility, resolve_procedure
from .database import REVIEW_ROLES, now_iso
from .errors import NotFoundError, ValidationError, WorkflowError
from .repository import record_event, row_to_rule

# 备案状态机
ST_DRAFT = "草稿"
ST_IN_REVIEW = "复核中"
ST_AUTHORIZED = "已授权"
ST_REJECTED = "已驳回"
ST_ARCHIVED = "已归档"


def run_completeness_check(conn: sqlite3.Connection, facility_id: str, procedure_ref: str) -> dict[str, Any]:
    """备案前置完整性检查：解析项目（按当前规则）并比对机构场地/设备/人员。"""
    facility = get_facility(conn, facility_id)
    procedure = resolve_procedure(conn, procedure_ref)
    rule = procedure["current_rule"]
    proc_status = procedure["status"]

    staff_roles: dict[str, int] = {}
    for member in facility["staff"]:
        staff_roles[member["role"]] = staff_roles.get(member["role"], 0) + int(member["count"])

    missing_site = sorted(set(rule["site_requirements"]) - set(facility["site_capability"]))
    missing_equipment = sorted(set(rule["required_equipment"]) - set(facility["equipment"]))
    missing_personnel = sorted(
        role for role in rule["required_personnel"] if staff_roles.get(role, 0) < 1
    )
    gaps = {
        "site_requirements": missing_site,
        "required_equipment": missing_equipment,
        "required_personnel": missing_personnel,
    }
    passed = (
        proc_status == "active"
        and not missing_site
        and not missing_equipment
        and not missing_personnel
    )
    reasons: list[str] = []
    if proc_status == "suspended":
        reasons.append("项目已紧急暂停，暂停期间不得新备案")
    elif proc_status == "split":
        reasons.append("项目已拆分，请按拆分后的新项目分别备案")
    if missing_site:
        reasons.append("缺少场地：" + "、".join(missing_site))
    if missing_equipment:
        reasons.append("缺少设备：" + "、".join(missing_equipment))
    if missing_personnel:
        reasons.append("缺少人员：" + "、".join(missing_personnel))
    return {
        "passed": passed,
        "facility_id": facility_id,
        "procedure_code": procedure["code"],
        "procedure_name": procedure["name"],
        "procedure_status": proc_status,
        "resolved_via": "alias" if procedure_ref.strip() not in (procedure["code"], procedure["name"]) else "direct",
        "rule_id": rule["id"],
        "risk_level": rule["risk_level"],
        "gaps": gaps,
        "reasons": reasons,
        "checked_at": now_iso(),
    }


def submit_filing(conn: sqlite3.Connection, facility_id: str, procedure_ref: str) -> dict[str, Any]:
    """提交备案：先强制运行完整性检查，通过后冻结规则快照并生成多角色复核任务。"""
    report = run_completeness_check(conn, facility_id, procedure_ref)
    if not report["passed"]:
        raise ValidationError("完整性检查未通过，备案不予受理", details=report)
    filing_id = f"BA-{uuid.uuid4().hex[:12].upper()}"
    ts = now_iso()
    conn.execute(
        "INSERT INTO filings(id, facility_id, procedure_code, rule_id, risk_level, status,"
        " check_report_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            filing_id,
            facility_id,
            report["procedure_code"],
            report["rule_id"],
            report["risk_level"],
            ST_IN_REVIEW,
            json.dumps(report, ensure_ascii=False),
            ts,
        ),
    )
    for seq, role in enumerate(REVIEW_ROLES):
        conn.execute(
            "INSERT INTO review_tasks(id, filing_id, role, sequence, status) VALUES(?,?,?,?, 'pending')",
            (f"RT-{uuid.uuid4().hex[:10]}", filing_id, role, seq),
        )
    record_event(
        conn,
        "filing",
        filing_id,
        "filing_submitted",
        {"facility_id": facility_id, "procedure_code": report["procedure_code"], "rule_id": report["rule_id"]},
    )
    conn.commit()
    return get_filing(conn, filing_id)


def _get_filing_row(conn: sqlite3.Connection, filing_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM filings WHERE id=?", (filing_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"备案不存在：{filing_id}")
    return row


def get_filing(conn: sqlite3.Connection, filing_id: str) -> dict[str, Any]:
    row = _get_filing_row(conn, filing_id)
    tasks = [
        {
            "role": r["role"],
            "sequence": r["sequence"],
            "status": r["status"],
            "reviewer": r["reviewer"],
            "decision": r["decision"],
            "comment": r["comment"],
            "decided_at": r["decided_at"],
        }
        for r in conn.execute(
            "SELECT * FROM review_tasks WHERE filing_id=? ORDER BY sequence", (filing_id,)
        )
    ]
    frozen_rule = row_to_rule(conn.execute("SELECT * FROM classification_rules WHERE id=?", (row["rule_id"],)).fetchone())
    services = [
        {
            "id": r["id"],
            "performed_at": r["performed_at"],
            "note": r["note"],
        }
        for r in conn.execute("SELECT * FROM services WHERE filing_id=? ORDER BY performed_at", (filing_id,))
    ]
    return {
        "id": row["id"],
        "facility_id": row["facility_id"],
        "procedure_code": row["procedure_code"],
        "status": row["status"],
        "decision": row["decision"],
        "risk_level": row["risk_level"],
        "frozen_rule": frozen_rule,
        "check_report": json.loads(row["check_report_json"]),
        "review_tasks": tasks,
        "services": services,
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


def list_filings(conn: sqlite3.Connection, facility_id: str | None = None) -> list[dict[str, Any]]:
    if facility_id:
        rows = conn.execute("SELECT id FROM filings WHERE facility_id=? ORDER BY created_at", (facility_id,))
    else:
        rows = conn.execute("SELECT id FROM filings ORDER BY created_at")
    return [get_filing(conn, r["id"]) for r in rows]


def review_filing(
    conn: sqlite3.Connection,
    filing_id: str,
    role: str,
    reviewer: str,
    approve: bool,
    comment: str = "",
) -> dict[str, Any]:
    """按角色顺序复核：前序角色通过后当前角色才能操作；任一驳回则备案终止。"""
    row = _get_filing_row(conn, filing_id)
    if row["status"] != ST_IN_REVIEW:
        raise WorkflowError(f"备案当前状态为 {row['status']}，不可再复核")
    task = conn.execute(
        "SELECT * FROM review_tasks WHERE filing_id=? AND role=?", (filing_id, role)
    ).fetchone()
    if task is None:
        raise ValidationError(f"角色 {role} 不在该备案的复核流程中")
    if task["status"] != "pending":
        raise WorkflowError(f"{role} 的复核已完成")
    blockers = conn.execute(
        "SELECT role FROM review_tasks WHERE filing_id=? AND status='pending' AND sequence<?",
        (filing_id, task["sequence"]),
    ).fetchall()
    if blockers:
        raise WorkflowError("须等待前序角色先复核：" + "、".join(r["role"] for r in blockers))

    ts = now_iso()
    decision = "approved" if approve else "rejected"
    conn.execute(
        "UPDATE review_tasks SET status='done', reviewer=?, decision=?, comment=?, decided_at=? WHERE id=?",
        (reviewer, decision, comment, ts, task["id"]),
    )
    new_status = ST_IN_REVIEW
    final_decision = None
    if not approve:
        new_status = ST_REJECTED
        final_decision = "驳回"
        # 关闭尚未处理的复核任务，流程终止
        conn.execute(
            "UPDATE review_tasks SET status='skipped' WHERE filing_id=? AND status='pending'",
            (filing_id,),
        )
    else:
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM review_tasks WHERE filing_id=? AND status='pending'", (filing_id,)
        ).fetchone()["c"]
        if remaining == 0:
            new_status = ST_AUTHORIZED
            final_decision = "授权"
    if new_status != ST_IN_REVIEW:
        conn.execute(
            "UPDATE filings SET status=?, decision=?, decided_at=? WHERE id=?",
            (new_status, final_decision, ts, filing_id),
        )
    record_event(
        conn,
        "filing",
        filing_id,
        "review_decided",
        {"role": role, "reviewer": reviewer, "decision": decision, "new_status": new_status},
    )
    conn.commit()
    return get_filing(conn, filing_id)


def archive_filing(conn: sqlite3.Connection, filing_id: str) -> dict[str, Any]:
    """授权备案到期后归档；归档后冻结快照仍可追溯。"""
    row = _get_filing_row(conn, filing_id)
    if row["status"] != ST_AUTHORIZED:
        raise WorkflowError(f"仅已授权备案可归档，当前状态：{row['status']}")
    ts = now_iso()
    conn.execute("UPDATE filings SET status=?, decided_at=? WHERE id=?", (ST_ARCHIVED, ts, filing_id))
    record_event(conn, "filing", filing_id, "filing_archived", {})
    conn.commit()
    return get_filing(conn, filing_id)


# ------------------------------------------------------------------ 历史服务


def register_service(
    conn: sqlite3.Connection,
    filing_id: str,
    performed_at: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """登记一次历史服务。服务永久指向备案时冻结的规则版本，后续规则变更不改写历史。"""
    row = _get_filing_row(conn, filing_id)
    if row["status"] not in (ST_AUTHORIZED, ST_ARCHIVED):
        raise WorkflowError(f"仅已授权/已归档备案可登记服务，当前状态：{row['status']}")
    service_id = f"SV-{uuid.uuid4().hex[:12].upper()}"
    ts = performed_at or now_iso()
    conn.execute(
        "INSERT INTO services(id, filing_id, facility_id, procedure_code, performed_at, note)"
        " VALUES(?,?,?,?,?,?)",
        (service_id, filing_id, row["facility_id"], row["procedure_code"], ts, note),
    )
    record_event(
        conn,
        "service",
        service_id,
        "service_registered",
        {"filing_id": filing_id, "rule_id": row["rule_id"], "performed_at": ts},
    )
    conn.commit()
    service = conn.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
    return {
        "id": service["id"],
        "filing_id": service["filing_id"],
        "facility_id": service["facility_id"],
        "procedure_code": service["procedure_code"],
        "performed_at": service["performed_at"],
        "note": service["note"],
        "frozen_rule_id": row["rule_id"],
        "frozen_risk_level": row["risk_level"],
    }


def get_service(conn: sqlite3.Connection, service_id: str) -> dict[str, Any]:
    """读取历史服务及其当时备案快照。"""
    service = conn.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
    if service is None:
        raise NotFoundError(f"历史服务不存在：{service_id}")
    filing = get_filing(conn, service["filing_id"])
    return {
        "id": service["id"],
        "performed_at": service["performed_at"],
        "note": service["note"],
        "filing": {
            "id": filing["id"],
            "facility_id": filing["facility_id"],
            "procedure_code": filing["procedure_code"],
            "frozen_rule_id": filing["frozen_rule"]["id"],
            "frozen_rule_version": filing["frozen_rule"]["version"],
            "frozen_risk_level": filing["risk_level"],
            "status": filing["status"],
        },
    }
