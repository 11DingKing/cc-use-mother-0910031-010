"""分级规则版本链：更正、拆分、依赖替代、紧急暂停、差异比较与影响分析。"""
from __future__ import annotations

import sqlite3
from typing import Any

from .database import RISK_LEVELS, now_iso
from .errors import ConflictError, NotFoundError, ValidationError, WorkflowError
from .repository import record_event, row_to_rule

CHANGE_TYPES = {"initial", "correction", "split", "split_derivative", "substitution", "suspension"}


def validate_rule_content(
    risk_level: str,
    site_requirements: list[str],
    required_equipment: list[str],
    required_personnel: list[str],
) -> None:
    if risk_level not in RISK_LEVELS:
        raise ValidationError(f"风险等级必须是：{'/'.join(RISK_LEVELS)}")
    for label, value in (
        ("场地要求", site_requirements),
        ("必备设备", required_equipment),
        ("必备人员", required_personnel),
    ):
        if not isinstance(value, list) or not value:
            raise ValidationError(f"{label}必须是非空列表")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValidationError(f"{label}条目不能为空字符串")
    if len(required_personnel) != len(set(required_personnel)):
        raise ValidationError("必备人员角色不能重复")


def get_rule(conn: sqlite3.Connection, rule_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM classification_rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"规则版本不存在：{rule_id}")
    return row_to_rule(row)


def list_rule_versions(conn: sqlite3.Connection, procedure_code: str) -> list[dict[str, Any]]:
    row = conn.execute("SELECT 1 FROM procedures WHERE code=?", (procedure_code,)).fetchone()
    if row is None:
        raise NotFoundError(f"项目不存在：{procedure_code}")
    rows = conn.execute(
        "SELECT * FROM classification_rules WHERE procedure_code=? ORDER BY version",
        (procedure_code,),
    ).fetchall()
    return [row_to_rule(r) for r in rows]


def _latest_rule_row(conn: sqlite3.Connection, procedure_code: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM classification_rules WHERE procedure_code=? ORDER BY version DESC LIMIT 1",
        (procedure_code,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"项目尚无分级规则：{procedure_code}")
    return row


def _publish_version(
    conn: sqlite3.Connection,
    procedure_code: str,
    content: dict[str, Any] | None,
    change_type: str,
    reason: str,
    *,
    substitutions: list[dict[str, str]] | None = None,
    payload_extra: dict[str, Any] | None = None,
    new_status: str | None = None,
) -> dict[str, Any]:
    """在当前版本之上发布新版本，维护 replaces/superseded 版本链。"""
    from .catalog import _get_procedure_row, _insert_rule

    _get_procedure_row(conn, procedure_code)
    previous = row_to_rule(_latest_rule_row(conn, procedure_code))
    if content is None:
        # 暂停、拆分占位版本沿用上一版分类条件
        content = {
            "risk_level": previous["risk_level"],
            "site_requirements": previous["site_requirements"],
            "required_equipment": previous["required_equipment"],
            "required_personnel": previous["required_personnel"],
        }
    ts = now_iso()
    rule = _insert_rule(
        conn,
        procedure_code=procedure_code,
        risk_level=content["risk_level"],
        site_requirements=content["site_requirements"],
        required_equipment=content["required_equipment"],
        required_personnel=content["required_personnel"],
        change_type=change_type,
        reason=reason,
        substitutions=substitutions,
        replaces_rule_id=previous["id"],
        effective_from=ts,
        payload_extra=payload_extra,
    )
    conn.execute(
        "UPDATE classification_rules SET superseded_by_rule_id=?, superseded_at=? WHERE id=?",
        (rule["id"], ts, previous["id"]),
    )
    conn.execute(
        "UPDATE procedures SET current_rule_id=?, status=COALESCE(?, status) WHERE code=?",
        (rule["id"], new_status, procedure_code),
    )
    record_event(
        conn,
        "procedure",
        procedure_code,
        f"rule_{change_type}_published",
        {"rule_id": rule["id"], "replaces": previous["id"], "reason": reason},
    )
    conn.commit()
    return rule


def correct_classification(
    conn: sqlite3.Connection,
    procedure_code: str,
    *,
    risk_level: str,
    site_requirements: list[str],
    required_equipment: list[str],
    required_personnel: list[str],
    reason: str,
) -> dict[str, Any]:
    """分类更正：发布新版本；若项目曾紧急暂停，随更正恢复。"""
    proc = conn.execute("SELECT status FROM procedures WHERE code=?", (procedure_code,)).fetchone()
    if proc is None:
        raise NotFoundError(f"项目不存在：{procedure_code}")
    new_status = "active" if proc["status"] == "suspended" else None
    return _publish_version(
        conn,
        procedure_code,
        {
            "risk_level": risk_level,
            "site_requirements": site_requirements,
            "required_equipment": required_equipment,
            "required_personnel": required_personnel,
        },
        "correction",
        reason,
        new_status=new_status,
    )


def substitute_dependencies(
    conn: sqlite3.Connection,
    procedure_code: str,
    substitutions: list[dict[str, str]],
    reason: str,
) -> dict[str, Any]:
    """设备/人员依赖替代：required_* 中旧依赖被新依赖替换，形成 substitution 版本。"""
    if not substitutions:
        raise ValidationError("替代清单不能为空")
    current = row_to_rule(_latest_rule_row(conn, procedure_code))
    equipment = list(current["required_equipment"])
    personnel = list(current["required_personnel"])
    normalized: list[dict[str, str]] = []
    for sub in substitutions:
        kind = sub.get("kind")
        old = (sub.get("from") or "").strip()
        new = (sub.get("to") or "").strip()
        if kind not in ("equipment", "personnel") or not old or not new:
            raise ValidationError("每条替代必须包含 kind(equipment/personnel)、from、to")
        pool = equipment if kind == "equipment" else personnel
        if old not in pool:
            raise ValidationError(f"当前必备{('设备' if kind == 'equipment' else '人员')}中不存在：{old}")
        pool[pool.index(old)] = new
        normalized.append({"kind": kind, "from": old, "to": new})
    return _publish_version(
        conn,
        procedure_code,
        {
            "risk_level": current["risk_level"],
            "site_requirements": current["site_requirements"],
            "required_equipment": equipment,
            "required_personnel": personnel,
        },
        "substitution",
        reason,
        substitutions=normalized,
    )


def emergency_suspend(conn: sqlite3.Connection, procedure_code: str, reason: str) -> dict[str, Any]:
    """紧急暂停：发布暂停版本并冻结新项目备案，历史备案与服务不受影响。"""
    if not reason:
        raise ValidationError("紧急暂停必须填写原因")
    proc = conn.execute("SELECT status FROM procedures WHERE code=?", (procedure_code,)).fetchone()
    if proc is None:
        raise NotFoundError(f"项目不存在：{procedure_code}")
    if proc["status"] == "suspended":
        raise ConflictError("项目已处于紧急暂停状态")
    return _publish_version(
        conn,
        procedure_code,
        None,
        "suspension",
        reason,
        new_status="suspended",
    )


def split_procedure(
    conn: sqlite3.Connection,
    old_code: str,
    new_procedures: list[dict[str, Any]],
    reason: str,
) -> dict[str, Any]:
    """项目拆分：原项目发布 split 版本并停用，新项目各自建档（V1 回链原规则）。"""
    if not new_procedures:
        raise ValidationError("拆分目标不能为空")
    old = conn.execute("SELECT * FROM procedures WHERE code=?", (old_code,)).fetchone()
    if old is None:
        raise NotFoundError(f"项目不存在：{old_code}")
    if old["status"] != "active":
        raise WorkflowError(f"项目当前状态 {old['status']} 不允许拆分")
    codes = [item["code"] for item in new_procedures]
    if len(codes) != len(set(codes)):
        raise ValidationError("拆分后的新项目编码不能重复")
    # 先完成全部校验，避免父项目已发布拆分版本后子项目建项失败
    for item in new_procedures:
        if conn.execute("SELECT 1 FROM procedures WHERE code=?", (item["code"],)).fetchone():
            raise ConflictError(f"项目编码已存在：{item['code']}")
        validate_rule_content(
            item["risk_level"],
            item["site_requirements"],
            item["required_equipment"],
            item["required_personnel"],
        )
        if not item.get("name"):
            raise ValidationError("拆分目标必须包含 name")

    parent_rule = row_to_rule(_latest_rule_row(conn, old_code))
    split_rule = _publish_version(
        conn,
        old_code,
        None,
        "split",
        reason,
        payload_extra={"split_into": codes},
        new_status="split",
    )

    from .catalog import _insert_rule

    ts = now_iso()
    results: list[dict[str, Any]] = []
    for item in new_procedures:
        conn.execute(
            "INSERT INTO procedures(code, name, status, created_at) VALUES(?,?, 'active', ?)",
            (item["code"], item["name"], ts),
        )
        rule = _insert_rule(
            conn,
            procedure_code=item["code"],
            risk_level=item["risk_level"],
            site_requirements=item["site_requirements"],
            required_equipment=item["required_equipment"],
            required_personnel=item["required_personnel"],
            change_type="split_derivative",
            reason=f"自 {old_code} 拆分：{reason}",
            substitutions=None,
            replaces_rule_id=parent_rule["id"],
            effective_from=ts,
        )
        conn.execute(
            "UPDATE procedures SET current_rule_id=? WHERE code=?", (rule["id"], item["code"])
        )
        for alias in item.get("aliases", []):
            conn.execute(
                "INSERT OR IGNORE INTO procedure_aliases(alias, procedure_code, created_at)"
                " VALUES(?,?,?)",
                (alias, item["code"], ts),
            )
        record_event(
            conn,
            "procedure",
            item["code"],
            "procedure_split_derivative_created",
            {"parent_code": old_code, "parent_rule_id": parent_rule["id"], "rule_id": rule["id"]},
        )
        results.append(rule)
    conn.commit()
    return {"split_rule": split_rule, "new_rules": results}


# ------------------------------------------------------------------ 差异比较


def _set_diff(before: list[str], after: list[str]) -> dict[str, list[str]]:
    return {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after))}


def diff_rules(conn: sqlite3.Connection, rule_id_a: str, rule_id_b: str) -> dict[str, Any]:
    """比较两个规则版本的分类差异。"""
    a = get_rule(conn, rule_id_a)
    b = get_rule(conn, rule_id_b)
    equipment_diff = _set_diff(a["required_equipment"], b["required_equipment"])
    personnel_diff = _set_diff(a["required_personnel"], b["required_personnel"])
    site_diff = _set_diff(a["site_requirements"], b["site_requirements"])
    equipment_diff["substitutions"] = [
        sub for sub in b.get("substitutions", []) if sub.get("kind") == "equipment"
    ]
    personnel_diff["substitutions"] = [
        sub for sub in b.get("substitutions", []) if sub.get("kind") == "personnel"
    ]
    return {
        "a": {"rule_id": a["id"], "procedure_code": a["procedure_code"], "version": a["version"]},
        "b": {"rule_id": b["id"], "procedure_code": b["procedure_code"], "version": b["version"]},
        "risk_level": {"from": a["risk_level"], "to": b["risk_level"], "changed": a["risk_level"] != b["risk_level"]},
        "site_requirements": site_diff,
        "required_equipment": equipment_diff,
        "required_personnel": personnel_diff,
        "change_types": {"a": a["change_type"], "b": b["change_type"]},
        "changed": (
            a["risk_level"] != b["risk_level"]
            or bool(site_diff["added"] or site_diff["removed"])
            or bool(equipment_diff["added"] or equipment_diff["removed"])
            or bool(personnel_diff["added"] or personnel_diff["removed"])
        ),
    }


# ------------------------------------------------------------------ 影响分析


def _facility_satisfies(facility: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    staff_roles: dict[str, int] = {}
    for member in facility["staff"]:
        staff_roles[member["role"]] = staff_roles.get(member["role"], 0) + int(member["count"])
    missing = {
        "site_requirements": sorted(set(rule["site_requirements"]) - set(facility["site_capability"])),
        "required_equipment": sorted(set(rule["required_equipment"]) - set(facility["equipment"])),
        "required_personnel": sorted(
            role for role in rule["required_personnel"] if staff_roles.get(role, 0) < 1
        ),
    }
    missing["complete"] = not (
        missing["site_requirements"] or missing["required_equipment"] or missing["required_personnel"]
    )
    return missing


def analyze_impact(conn: sqlite3.Connection, procedure_code: str) -> dict[str, Any]:
    """对比当前最新规则与该项目所有已授权机构，列出受影响授权。"""
    latest = row_to_rule(_latest_rule_row(conn, procedure_code))
    rows = conn.execute(
        "SELECT * FROM filings WHERE procedure_code=? AND status IN ('已授权','已归档') ORDER BY id",
        (procedure_code,),
    ).fetchall()
    affected: list[dict[str, Any]] = []
    for row in rows:
        facility = _facility_simple(conn, row["facility_id"])
        gap = _facility_satisfies(facility, latest)
        frozen = get_rule(conn, row["rule_id"])
        reasons: list[str] = []
        if latest["change_type"] == "suspension":
            reasons.append("项目已紧急暂停，授权不可继续使用")
        elif latest["change_type"] == "split":
            reasons.append("项目已拆分，授权不再对应现行项目")
        if frozen["risk_level"] != latest["risk_level"]:
            reasons.append(f"风险等级 {frozen['risk_level']} → {latest['risk_level']}")
        if gap["site_requirements"]:
            reasons.append("场地不满足新规则")
        if gap["required_equipment"]:
            reasons.append("设备不满足新规则")
        if gap["required_personnel"]:
            reasons.append("人员不满足新规则")
        affected.append(
            {
                "filing_id": row["id"],
                "facility_id": facility["id"],
                "facility_name": facility["name"],
                "frozen_rule_id": frozen["id"],
                "frozen_risk_level": frozen["risk_level"],
                "satisfies_latest": gap["complete"] and latest["change_type"] not in ("suspension", "split"),
                "missing": {k: v for k, v in gap.items() if k != "complete"},
                "reasons": reasons,
                "impacted": bool(reasons),
            }
        )
    return {
        "procedure_code": procedure_code,
        "latest_rule_id": latest["id"],
        "latest_version": latest["version"],
        "change_type": latest["change_type"],
        "risk_level": latest["risk_level"],
        "affected_authorization_count": sum(1 for item in affected if item["impacted"]),
        "authorizations": affected,
    }


def _facility_simple(conn: sqlite3.Connection, facility_id: str) -> dict[str, Any]:
    from .catalog import get_facility

    return get_facility(conn, facility_id)
