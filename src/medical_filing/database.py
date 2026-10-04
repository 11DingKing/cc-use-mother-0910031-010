"""SQLite 连接、建表与示例数据。

只使用标准库，便于在正式容器内直接运行。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS facilities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    site_capability TEXT NOT NULL DEFAULT '[]',
    staff_json TEXT NOT NULL DEFAULT '[]',
    equipment_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS procedures (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    current_rule_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS procedure_aliases (
    alias TEXT PRIMARY KEY COLLATE NOCASE,
    procedure_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (procedure_code) REFERENCES procedures(code)
);

CREATE TABLE IF NOT EXISTS classification_rules (
    id TEXT PRIMARY KEY,
    procedure_code TEXT NOT NULL,
    version INTEGER NOT NULL,
    risk_level TEXT NOT NULL,
    site_requirements TEXT NOT NULL DEFAULT '[]',
    required_equipment TEXT NOT NULL DEFAULT '[]',
    required_personnel TEXT NOT NULL DEFAULT '[]',
    change_type TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    substitutions_json TEXT NOT NULL DEFAULT '[]',
    replaces_rule_id TEXT,
    superseded_by_rule_id TEXT,
    effective_from TEXT NOT NULL,
    superseded_at TEXT,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (procedure_code) REFERENCES procedures(code)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rule_version
    ON classification_rules(procedure_code, version);

CREATE TABLE IF NOT EXISTS filings (
    id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL,
    procedure_code TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT,
    check_report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY (facility_id) REFERENCES facilities(id),
    FOREIGN KEY (rule_id) REFERENCES classification_rules(id)
);

CREATE TABLE IF NOT EXISTS review_tasks (
    id TEXT PRIMARY KEY,
    filing_id TEXT NOT NULL,
    role TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewer TEXT,
    decision TEXT,
    comment TEXT NOT NULL DEFAULT '',
    decided_at TEXT,
    UNIQUE(filing_id, role),
    FOREIGN KEY (filing_id) REFERENCES filings(id)
);

CREATE TABLE IF NOT EXISTS services (
    id TEXT PRIMARY KEY,
    filing_id TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    procedure_code TEXT NOT NULL,
    performed_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (filing_id) REFERENCES filings(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""

# 多角色复核顺序：先执业人员核验技术条件，再复核专家评定风险，最后监管人员决定
REVIEW_ROLES = ["执业人员", "复核专家", "监管人员"]

RISK_LEVELS = ["一级", "二级", "三级", "四级"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path = ":memory:", check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def load_seed(conn: sqlite3.Connection) -> None:
    """写入贴近医美的示例数据：名称相近但分级不同的项目 + 两家机构。"""
    ts = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO facilities(id, name, site_capability, staff_json, equipment_json, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            "F001",
            "星颜医疗美容门诊部",
            json.dumps(["普通手术室", "独立消毒间", "留观室"], ensure_ascii=False),
            json.dumps(
                [
                    {"role": "主诊医师", "name": "林医生", "count": 2},
                    {"role": "麻醉医师", "name": "周医生", "count": 1},
                    {"role": "护士", "name": "", "count": 4},
                ],
                ensure_ascii=False,
            ),
            json.dumps(["监护仪", "吸脂机", "电凝止血设备"], ensure_ascii=False),
            ts,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO facilities(id, name, site_capability, staff_json, equipment_json, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            "F002",
            "悦己美容诊所",
            json.dumps(["独立治疗室"], ensure_ascii=False),
            json.dumps([{"role": "主诊医师", "name": "陈医生", "count": 1}], ensure_ascii=False),
            json.dumps(["红蓝光治疗仪"], ensure_ascii=False),
            ts,
        ),
    )
    seed_rules = [
        # (code, name, aliases, risk, site, equipment, personnel)
        (
            "P001",
            "负压脂肪抽吸术",
            ["吸脂手术", "抽脂术"],
            "三级",
            ["普通手术室", "独立消毒间", "留观室"],
            ["监护仪", "吸脂机", "电凝止血设备"],
            ["主诊医师", "麻醉医师", "护士"],
        ),
        (
            "P002",
            "脂肪重塑术（射频溶脂）",
            ["射频溶脂", "黄金微雕溶脂"],
            "二级",
            ["独立治疗室"],
            ["射频溶脂设备"],
            ["主诊医师", "护士"],
        ),
        (
            "P003",
            "红蓝光祛痘治疗",
            ["红蓝光", "光动力祛痘"],
            "一级",
            ["独立治疗室"],
            ["红蓝光治疗仪"],
            ["主诊医师"],
        ),
    ]
    for idx, (code, name, aliases, risk, site, equip, personnel) in enumerate(seed_rules, start=1):
        row = conn.execute("SELECT 1 FROM procedures WHERE code=?", (code,)).fetchone()
        if row:
            continue
        conn.execute(
            "INSERT INTO procedures(code, name, status, created_at) VALUES(?,?, 'active', ?)",
            (code, name, ts),
        )
        rule_id = f"R-{code}-V1"
        payload = {
            "risk_level": risk,
            "site_requirements": site,
            "required_equipment": equip,
            "required_personnel": personnel,
        }
        conn.execute(
            "INSERT INTO classification_rules(id, procedure_code, version, risk_level,"
            " site_requirements, required_equipment, required_personnel,"
            " change_type, reason, substitutions_json, effective_from, payload_json)"
            " VALUES(?,?,?,?,"
            "?,?,?,'initial','首次发布','[]',?,?)",
            (
                rule_id,
                code,
                1,
                risk,
                json.dumps(site, ensure_ascii=False),
                json.dumps(equip, ensure_ascii=False),
                json.dumps(personnel, ensure_ascii=False),
                ts,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        conn.execute(
            "UPDATE procedures SET current_rule_id=? WHERE code=?", (rule_id, code)
        )
        for alias in aliases:
            conn.execute(
                "INSERT OR IGNORE INTO procedure_aliases(alias, procedure_code, created_at)"
                " VALUES(?,?,?)",
                (alias, code, ts),
            )
    conn.commit()
