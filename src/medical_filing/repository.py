"""仓储辅助函数：行转字典与事件记录。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .database import now_iso


def row_to_rule(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "procedure_code": row["procedure_code"],
        "version": row["version"],
        "risk_level": row["risk_level"],
        "site_requirements": json.loads(row["site_requirements"]),
        "required_equipment": json.loads(row["required_equipment"]),
        "required_personnel": json.loads(row["required_personnel"]),
        "change_type": row["change_type"],
        "reason": row["reason"],
        "substitutions": json.loads(row["substitutions_json"]),
        "replaces_rule_id": row["replaces_rule_id"],
        "superseded_by_rule_id": row["superseded_by_rule_id"],
        "effective_from": row["effective_from"],
        "superseded_at": row["superseded_at"],
    }


def record_event(
    conn: sqlite3.Connection,
    stream: str,
    stream_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO events(stream, stream_id, event_type, payload_json, created_at)"
        " VALUES(?,?,?,?,?)",
        (stream, stream_id, event_type, json.dumps(payload, ensure_ascii=False), now_iso()),
    )
