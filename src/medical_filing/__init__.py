"""医美项目分级备案后端服务包。

核心能力：
- 项目定义与别名目录（catalog）
- 分级规则版本链：更正 / 拆分 / 依赖替代 / 紧急暂停（rules）
- 备案完整性检查与多角色有序复核、历史服务冻结（filings）
- 分类差异比较与规则变更影响分析（rules）
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import catalog, filings, rules
from .database import connect, load_seed


class FilingService:
    """面向应用层的单一入口，持有一个 SQLite 连接。

    HTTP 服务为多线程模型，SQLite 连接允许跨线程并由 lock 串行化访问。
    """

    def __init__(self, db_path: str | Path = ":memory:", seed: bool = False) -> None:
        self.lock = threading.RLock()
        self.conn: sqlite3.Connection = connect(db_path, check_same_thread=False)
        if seed:
            load_seed(self.conn)

    def close(self) -> None:
        self.conn.close()

    # ---- 项目目录 ----
    def create_procedure(self, **kwargs: Any) -> dict[str, Any]:
        return catalog.create_procedure(self.conn, **kwargs)

    def get_procedure(self, code: str) -> dict[str, Any]:
        return catalog.get_procedure(self.conn, code)

    def list_procedures(self, include_suspended: bool = True) -> list[dict[str, Any]]:
        return catalog.list_procedures(self.conn, include_suspended)

    def resolve_procedure(self, name_or_code: str) -> dict[str, Any]:
        return catalog.resolve_procedure(self.conn, name_or_code)

    def add_alias(self, code: str, alias: str) -> None:
        catalog.add_alias(self.conn, code, alias)

    def remove_alias(self, alias: str) -> None:
        catalog.remove_alias(self.conn, alias)

    # ---- 机构 ----
    def register_facility(self, **kwargs: Any) -> dict[str, Any]:
        return catalog.register_facility(self.conn, **kwargs)

    def get_facility(self, facility_id: str) -> dict[str, Any]:
        return catalog.get_facility(self.conn, facility_id)

    def update_facility_resources(self, facility_id: str, **kwargs: Any) -> dict[str, Any]:
        return catalog.update_facility_resources(self.conn, facility_id, **kwargs)

    # ---- 规则版本链 ----
    def correct_classification(self, code: str, **kwargs: Any) -> dict[str, Any]:
        return rules.correct_classification(self.conn, code, **kwargs)

    def substitute_dependencies(self, code: str, substitutions: list[dict[str, str]], reason: str) -> dict[str, Any]:
        return rules.substitute_dependencies(self.conn, code, substitutions, reason)

    def emergency_suspend(self, code: str, reason: str) -> dict[str, Any]:
        return rules.emergency_suspend(self.conn, code, reason)

    def split_procedure(self, code: str, new_procedures: list[dict[str, Any]], reason: str) -> dict[str, Any]:
        return rules.split_procedure(self.conn, code, new_procedures, reason)

    def list_rule_versions(self, code: str) -> list[dict[str, Any]]:
        return rules.list_rule_versions(self.conn, code)

    def get_rule(self, rule_id: str) -> dict[str, Any]:
        return rules.get_rule(self.conn, rule_id)

    def diff_rules(self, rule_id_a: str, rule_id_b: str) -> dict[str, Any]:
        return rules.diff_rules(self.conn, rule_id_a, rule_id_b)

    def analyze_impact(self, code: str) -> dict[str, Any]:
        return rules.analyze_impact(self.conn, code)

    # ---- 备案 ----
    def run_completeness_check(self, facility_id: str, procedure_ref: str) -> dict[str, Any]:
        return filings.run_completeness_check(self.conn, facility_id, procedure_ref)

    def submit_filing(self, facility_id: str, procedure_ref: str) -> dict[str, Any]:
        return filings.submit_filing(self.conn, facility_id, procedure_ref)

    def get_filing(self, filing_id: str) -> dict[str, Any]:
        return filings.get_filing(self.conn, filing_id)

    def list_filings(self, facility_id: str | None = None) -> list[dict[str, Any]]:
        return filings.list_filings(self.conn, facility_id)

    def review_filing(self, filing_id: str, role: str, reviewer: str, approve: bool, comment: str = "") -> dict[str, Any]:
        return filings.review_filing(self.conn, filing_id, role, reviewer, approve, comment)

    def archive_filing(self, filing_id: str) -> dict[str, Any]:
        return filings.archive_filing(self.conn, filing_id)

    def register_service(self, filing_id: str, performed_at: str | None = None, note: str = "") -> dict[str, Any]:
        return filings.register_service(self.conn, filing_id, performed_at, note)

    def get_service(self, service_id: str) -> dict[str, Any]:
        return filings.get_service(self.conn, service_id)


__all__ = [
    "FilingService",
    "connect",
    "load_seed",
    "catalog",
    "rules",
    "filings",
]
