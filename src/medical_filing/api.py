"""基于标准库 http.server 的 REST 接口。

启动：python3 -m medical_filing.api --db data/filing.db --seed --port 8000
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs

from . import FilingService
from .errors import DomainError

Route = tuple[str, re.Pattern[str], Callable[..., Any]]


class ApiHandler(BaseHTTPRequestHandler):
    service: FilingService  # 由 make_server 注入到类上

    server_version = "MedicalFilingHTTP/1.0"

    # ------------------------------------------------------------ 基础工具
    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return value

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return

    # ------------------------------------------------------------ 路由分发
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        with self.service.lock:
            try:
                for verb, pattern, handler in ROUTES:
                    if verb != method:
                        continue
                    match = pattern.fullmatch(path)
                    if match:
                        body = self._read_json() if method in ("POST", "DELETE", "PATCH") else {}
                        result = handler(self, query=query, body=body, **match.groupdict())
                        if result is None:
                            return
                        status, payload = result
                        self._send(status, payload)
                        return
                self._send(404, {"error": "not_found", "message": f"无此接口：{method} {path}"})
            except DomainError as exc:
                self.service.conn.rollback()
                payload: dict[str, Any] = {"error": exc.code, "message": str(exc)}
                details = getattr(exc, "details", None)
                if details is not None:
                    payload["details"] = details
                self._send(exc.status_code, payload)
            except Exception as exc:  # noqa: BLE001 - 兜底为 JSON 500
                self.service.conn.rollback()
                self._send(500, {"error": "internal_error", "message": str(exc)})

    # ------------------------------------------------------------ 项目目录
    def api_create_procedure(self, body: dict[str, Any], **_: Any) -> Any:
        required = ["code", "name", "risk_level", "site_requirements", "required_equipment", "required_personnel"]
        missing = [k for k in required if not body.get(k)]
        if missing:
            from .errors import ValidationError

            raise ValidationError("缺少字段：" + "、".join(missing))
        result = self.service.create_procedure(
            code=body["code"],
            name=body["name"],
            risk_level=body["risk_level"],
            site_requirements=body["site_requirements"],
            required_equipment=body["required_equipment"],
            required_personnel=body["required_personnel"],
            aliases=body.get("aliases") or [],
            reason=body.get("reason", "首次发布"),
        )
        return 201, result

    def api_list_procedures(self, query: dict[str, list[str]], **_: Any) -> Any:
        include_suspended = query.get("include_suspended", ["true"])[0] != "false"
        return 200, {"procedures": self.service.list_procedures(include_suspended)}

    def api_get_procedure(self, code: str, **_: Any) -> Any:
        return 200, self.service.get_procedure(code)

    def api_resolve_procedure(self, query: dict[str, list[str]], **_: Any) -> Any:
        ref = query.get("ref", [""])[0]
        return 200, self.service.resolve_procedure(ref)

    def api_add_alias(self, code: str, body: dict[str, Any], **_: Any) -> Any:
        self.service.add_alias(code, body.get("alias", ""))
        return 201, self.service.get_procedure(code)

    def api_remove_alias(self, body: dict[str, Any], **_: Any) -> Any:
        self.service.remove_alias(body.get("alias", ""))
        return 200, {"removed": body.get("alias")}

    # ------------------------------------------------------------ 机构
    def api_register_facility(self, body: dict[str, Any], **_: Any) -> Any:
        result = self.service.register_facility(
            facility_id=body["facility_id"],
            name=body["name"],
            site_capability=body.get("site_capability") or [],
            staff=body.get("staff") or [],
            equipment=body.get("equipment") or [],
        )
        return 201, result

    def api_get_facility(self, facility_id: str, **_: Any) -> Any:
        return 200, self.service.get_facility(facility_id)

    def api_update_facility(self, facility_id: str, body: dict[str, Any], **_: Any) -> Any:
        result = self.service.update_facility_resources(
            facility_id,
            site_capability=body.get("site_capability"),
            staff=body.get("staff"),
            equipment=body.get("equipment"),
        )
        return 200, result

    # ------------------------------------------------------------ 规则版本链
    def api_correct(self, code: str, body: dict[str, Any], **_: Any) -> Any:
        rule = self.service.correct_classification(
            code,
            risk_level=body["risk_level"],
            site_requirements=body["site_requirements"],
            required_equipment=body["required_equipment"],
            required_personnel=body["required_personnel"],
            reason=body.get("reason", ""),
        )
        return 201, rule

    def api_substitute(self, code: str, body: dict[str, Any], **_: Any) -> Any:
        rule = self.service.substitute_dependencies(
            code, body.get("substitutions") or [], body.get("reason", "")
        )
        return 201, rule

    def api_suspend(self, code: str, body: dict[str, Any], **_: Any) -> Any:
        rule = self.service.emergency_suspend(code, body.get("reason", ""))
        return 201, rule

    def api_split(self, code: str, body: dict[str, Any], **_: Any) -> Any:
        result = self.service.split_procedure(code, body.get("new_procedures") or [], body.get("reason", ""))
        return 201, result

    def api_list_rules(self, code: str, **_: Any) -> Any:
        return 200, {"procedure_code": code, "versions": self.service.list_rule_versions(code)}

    def api_get_rule(self, rule_id: str, **_: Any) -> Any:
        return 200, self.service.get_rule(rule_id)

    def api_diff(self, query: dict[str, list[str]], **_: Any) -> Any:
        a = query.get("a", [""])[0]
        b = query.get("b", [""])[0]
        return 200, self.service.diff_rules(a, b)

    def api_impact(self, code: str, **_: Any) -> Any:
        return 200, self.service.analyze_impact(code)

    # ------------------------------------------------------------ 备案
    def api_check(self, body: dict[str, Any], **_: Any) -> Any:
        return 200, self.service.run_completeness_check(body["facility_id"], body["procedure_ref"])

    def api_submit_filing(self, body: dict[str, Any], **_: Any) -> Any:
        return 201, self.service.submit_filing(body["facility_id"], body["procedure_ref"])

    def api_list_filings(self, query: dict[str, list[str]], **_: Any) -> Any:
        facility_id = query.get("facility_id", [None])[0]
        return 200, {"filings": self.service.list_filings(facility_id)}

    def api_get_filing(self, filing_id: str, **_: Any) -> Any:
        return 200, self.service.get_filing(filing_id)

    def api_review(self, filing_id: str, body: dict[str, Any], **_: Any) -> Any:
        return 200, self.service.review_filing(
            filing_id,
            role=body["role"],
            reviewer=body.get("reviewer", body["role"]),
            approve=bool(body.get("approve")),
            comment=body.get("comment", ""),
        )

    def api_archive(self, filing_id: str, **_: Any) -> Any:
        return 200, self.service.archive_filing(filing_id)

    def api_register_service(self, filing_id: str, body: dict[str, Any], **_: Any) -> Any:
        return 201, self.service.register_service(
            filing_id, performed_at=body.get("performed_at"), note=body.get("note", "")
        )

    def api_get_service(self, service_id: str, **_: Any) -> Any:
        return 200, self.service.get_service(service_id)


def _routes(handler: type[ApiHandler]) -> list[Route]:
    p = re.compile
    return [
        ("POST", p(r"/api/procedures"), handler.api_create_procedure),
        ("GET", p(r"/api/procedures"), handler.api_list_procedures),
        ("GET", p(r"/api/procedures/resolve"), handler.api_resolve_procedure),
        ("POST", p(r"/api/aliases/remove"), handler.api_remove_alias),
        ("POST", p(r"/api/facilities"), handler.api_register_facility),
        ("POST", p(r"/api/completeness-checks"), handler.api_check),
        ("POST", p(r"/api/filings"), handler.api_submit_filing),
        ("GET", p(r"/api/filings"), handler.api_list_filings),
        ("GET", p(r"/api/rules/diff"), handler.api_diff),
        ("GET", p(r"/api/procedures/(?P<code>[^/]+)"), handler.api_get_procedure),
        ("POST", p(r"/api/procedures/(?P<code>[^/]+)/aliases"), handler.api_add_alias),
        ("POST", p(r"/api/procedures/(?P<code>[^/]+)/corrections"), handler.api_correct),
        ("POST", p(r"/api/procedures/(?P<code>[^/]+)/substitutions"), handler.api_substitute),
        ("POST", p(r"/api/procedures/(?P<code>[^/]+)/suspension"), handler.api_suspend),
        ("POST", p(r"/api/procedures/(?P<code>[^/]+)/splits"), handler.api_split),
        ("GET", p(r"/api/procedures/(?P<code>[^/]+)/rules"), handler.api_list_rules),
        ("GET", p(r"/api/procedures/(?P<code>[^/]+)/impact"), handler.api_impact),
        ("GET", p(r"/api/rules/(?P<rule_id>[^/]+)"), handler.api_get_rule),
        ("GET", p(r"/api/facilities/(?P<facility_id>[^/]+)"), handler.api_get_facility),
        ("PATCH", p(r"/api/facilities/(?P<facility_id>[^/]+)"), handler.api_update_facility),
        ("GET", p(r"/api/filings/(?P<filing_id>[^/]+)"), handler.api_get_filing),
        ("POST", p(r"/api/filings/(?P<filing_id>[^/]+)/reviews"), handler.api_review),
        ("POST", p(r"/api/filings/(?P<filing_id>[^/]+)/archive"), handler.api_archive),
        ("POST", p(r"/api/filings/(?P<filing_id>[^/]+)/services"), handler.api_register_service),
        ("GET", p(r"/api/services/(?P<service_id>[^/]+)"), handler.api_get_service),
    ]


ROUTES: list[Route] = _routes(ApiHandler)


def make_server(host: str, port: int, db_path: str, seed: bool = False) -> ThreadingHTTPServer:
    ApiHandler.service = FilingService(db_path, seed=seed)
    server = ThreadingHTTPServer((host, port), ApiHandler)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="医美项目分级备案后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="data/filing.db")
    parser.add_argument("--seed", action="store_true", help="写入示例项目与机构")
    args = parser.parse_args()
    if args.db != ":memory:":
        from pathlib import Path

        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    server = make_server(args.host, args.port, args.db, seed=args.seed)
    print(f"医美分级备案服务已启动：http://{args.host}:{args.port}  (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ApiHandler.service.close()
        server.server_close()


if __name__ == "__main__":
    main()
