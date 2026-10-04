"""项目目录与机构资源服务：项目定义、别名解析、机构设备人员维护。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .database import now_iso
from .errors import ConflictError, NotFoundError, ValidationError
from .repository import record_event, row_to_rule


def _get_procedure_row(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM procedures WHERE code=?", (code,)).fetchone()
    if row is None:
        raise NotFoundError(f"项目不存在：{code}")
    return row


def create_procedure(
    conn: sqlite3.Connection,
    code: str,
    name: str,
    risk_level: str,
    site_requirements: list[str],
    required_equipment: list[str],
    required_personnel: list[str],
    aliases: list[str] | None = None,
    reason: str = "首次发布",
) -> dict[str, Any]:
    """登记项目并发布其 V1 分级规则。"""
    if not code or not name:
        raise ValidationError("项目编码与名称不能为空")
    if conn.execute("SELECT 1 FROM procedures WHERE code=?", (code,)).fetchone():
        raise ConflictError(f"项目编码已存在：{code}")
    ts = now_iso()
    conn.execute(
        "INSERT INTO procedures(code, name, status, created_at) VALUES(?,?, 'active', ?)",
        (code, name, ts),
    )
    rule = _insert_rule(
        conn,
        procedure_code=code,
        risk_level=risk_level,
        site_requirements=site_requirements,
        required_equipment=required_equipment,
        required_personnel=required_personnel,
        change_type="initial",
        reason=reason,
        substitutions=None,
        replaces_rule_id=None,
        effective_from=ts,
    )
    conn.execute("UPDATE procedures SET current_rule_id=? WHERE code=?", (rule["id"], code))
    for alias in aliases or []:
        add_alias(conn, code, alias)
    record_event(conn, "procedure", code, "procedure_created", {"name": name, "rule_id": rule["id"]})
    conn.commit()
    return get_procedure(conn, code)


def _insert_rule(
    conn: sqlite3.Connection,
    *,
    procedure_code: str,
    risk_level: str,
    site_requirements: list[str],
    required_equipment: list[str],
    required_personnel: list[str],
    change_type: str,
    reason: str,
    substitutions: list[dict[str, str]] | None = None,
    replaces_rule_id: str | None,
    effective_from: str,
    payload_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .rules import validate_rule_content

    validate_rule_content(risk_level, site_requirements, required_equipment, required_personnel)
    ver_row = conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM classification_rules WHERE procedure_code=?",
        (procedure_code,),
    ).fetchone()
    version = ver_row["v"] + 1
    rule_id = f"R-{procedure_code}-V{version}"
    payload = {
        "risk_level": risk_level,
        "site_requirements": site_requirements,
        "required_equipment": required_equipment,
        "required_personnel": required_personnel,
    }
    if payload_extra:
        payload.update(payload_extra)
    conn.execute(
        "INSERT INTO classification_rules(id, procedure_code, version, risk_level,"
        " site_requirements, required_equipment, required_personnel,"
        " change_type, reason, substitutions_json, replaces_rule_id, effective_from, payload_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rule_id,
            procedure_code,
            version,
            risk_level,
            json.dumps(site_requirements, ensure_ascii=False),
            json.dumps(required_equipment, ensure_ascii=False),
            json.dumps(required_personnel, ensure_ascii=False),
            change_type,
            reason,
            json.dumps(substitutions or [], ensure_ascii=False),
            replaces_rule_id,
            effective_from,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    return row_to_rule(conn.execute("SELECT * FROM classification_rules WHERE id=?", (rule_id,)).fetchone())


def get_procedure(conn: sqlite3.Connection, code: str) -> dict[str, Any]:
    row = _get_procedure_row(conn, code)
    rule = None
    if row["current_rule_id"]:
        r = conn.execute(
            "SELECT * FROM classification_rules WHERE id=?", (row["current_rule_id"],)
        ).fetchone()
        rule = row_to_rule(r)
    aliases = [
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM procedure_aliases WHERE procedure_code=? ORDER BY alias", (code,)
        )
    ]
    return {
        "code": row["code"],
        "name": row["name"],
        "status": row["status"],
        "current_rule": rule,
        "aliases": aliases,
    }


def list_procedures(conn: sqlite3.Connection, include_suspended: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT code FROM procedures"
    if not include_suspended:
        sql += " WHERE status='active'"
    sql += " ORDER BY code"
    return [get_procedure(conn, r["code"]) for r in conn.execute(sql)]


def resolve_procedure(conn: sqlite3.Connection, name_or_code: str) -> dict[str, Any]:
    """按编码、正式名称或别名解析项目；名称相近的项目必须精确命中。"""
    key = (name_or_code or "").strip()
    if not key:
        raise ValidationError("项目标识不能为空")
    row = conn.execute("SELECT * FROM procedures WHERE code=?", (key,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM procedures WHERE name=?", (key,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT p.* FROM procedures p JOIN procedure_aliases a ON a.procedure_code=p.code"
            " WHERE a.alias=?",
            (key,),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"无法解析项目（编码/名称/别名）：{key}")
    return get_procedure(conn, row["code"])


def add_alias(conn: sqlite3.Connection, code: str, alias: str) -> None:
    _get_procedure_row(conn, code)
    alias = (alias or "").strip()
    if not alias:
        raise ValidationError("别名不能为空")
    owner = conn.execute(
        "SELECT procedure_code FROM procedure_aliases WHERE alias=?", (alias,)
    ).fetchone()
    if owner is not None:
        if owner["procedure_code"] == code:
            return
        raise ConflictError(f"别名已被项目 {owner['procedure_code']} 占用：{alias}")
    conn.execute(
        "INSERT INTO procedure_aliases(alias, procedure_code, created_at) VALUES(?,?,?)",
        (alias, code, now_iso()),
    )
    record_event(conn, "procedure", code, "alias_added", {"alias": alias})
    conn.commit()


def remove_alias(conn: sqlite3.Connection, alias: str) -> None:
    cur = conn.execute("DELETE FROM procedure_aliases WHERE alias=?", (alias,))
    if cur.rowcount == 0:
        raise NotFoundError(f"别名不存在：{alias}")
    conn.commit()


# ------------------------------------------------------------------ 机构资源


def register_facility(
    conn: sqlite3.Connection,
    facility_id: str,
    name: str,
    site_capability: list[str] | None = None,
    staff: list[dict[str, Any]] | None = None,
    equipment: list[str] | None = None,
) -> dict[str, Any]:
    if not facility_id or not name:
        raise ValidationError("机构编码与名称不能为空")
    if conn.execute("SELECT 1 FROM facilities WHERE id=?", (facility_id,)).fetchone():
        raise ConflictError(f"机构已存在：{facility_id}")
    conn.execute(
        "INSERT INTO facilities(id, name, site_capability, staff_json, equipment_json, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (
            facility_id,
            name,
            json.dumps(site_capability or [], ensure_ascii=False),
            json.dumps(staff or [], ensure_ascii=False),
            json.dumps(equipment or [], ensure_ascii=False),
            now_iso(),
        ),
    )
    record_event(conn, "facility", facility_id, "facility_registered", {"name": name})
    conn.commit()
    return get_facility(conn, facility_id)


def get_facility(conn: sqlite3.Connection, facility_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM facilities WHERE id=?", (facility_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"机构不存在：{facility_id}")
    return {
        "id": row["id"],
        "name": row["name"],
        "site_capability": json.loads(row["site_capability"]),
        "staff": json.loads(row["staff_json"]),
        "equipment": json.loads(row["equipment_json"]),
    }


def update_facility_resources(
    conn: sqlite3.Connection,
    facility_id: str,
    *,
    site_capability: list[str] | None = None,
    staff: list[dict[str, Any]] | None = None,
    equipment: list[str] | None = None,
) -> dict[str, Any]:
    current = get_facility(conn, facility_id)
    site = current["site_capability"] if site_capability is None else site_capability
    new_staff = current["staff"] if staff is None else staff
    equip = current["equipment"] if equipment is None else equipment
    for member in new_staff:
        if "role" not in member or "count" not in member:
            raise ValidationError("人员条目必须包含 role 与 count")
        if not isinstance(member["count"], int) or member["count"] < 0:
            raise ValidationError("人员数量必须是非负整数")
    conn.execute(
        "UPDATE facilities SET site_capability=?, staff_json=?, equipment_json=? WHERE id=?",
        (
            json.dumps(site, ensure_ascii=False),
            json.dumps(new_staff, ensure_ascii=False),
            json.dumps(equip, ensure_ascii=False),
            facility_id,
        ),
    )
    record_event(
        conn,
        "facility",
        facility_id,
        "facility_resources_updated",
        {"site_capability": site, "equipment": equip},
    )
    conn.commit()
    return get_facility(conn, facility_id)
