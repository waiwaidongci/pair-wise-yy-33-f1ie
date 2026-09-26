#!/usr/bin/env python3
"""Entry module (入口): HTTP 路由与启动。业务判定在 service.py / vouchers.py，存档在 store.py。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Backwards-compatible re-exports: `from app import GridService, Store, ApiError`
from service import GridService
from store import DB_PATH, ApiError, Store


class Handler(BaseHTTPRequestHandler):
    service: GridService

    def log_message(self, fmt: str, *args: object) -> None: sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))
    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        try: return json.loads(self.rfile.read(size)) if size else {}
        except json.JSONDecodeError as exc: raise ApiError(400, "JSON 请求体无效") from exc
    def _parts(self) -> list[str]: return [p for p in urlparse(self.path).path.strip("/").split("/") if p]

    def do_GET(self) -> None:
        try:
            p = self._parts()
            if p in (["health"], ["api", "health"]): out = {"status": "ok"}
            elif p == ["api", "state"]: out = self.service.state()
            elif len(p) == 3 and p[:2] == ["api", "plans"]: out = self.service.plan_detail(int(p[2]))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "vouchers": out = self.service.plan_vouchers(int(p[2]))
            elif not p:
                page = (Path(__file__).parent / "static" / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page); return
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send(exc.status, {"error": exc.message})
        except Exception as exc: self._send(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            p, b = self._parts(), self._body(); actor, role = self.headers.get("X-Actor"), self.headers.get("X-Role")
            if p == ["api", "assets"]: out = self.service.register_asset(actor, role, b.get("code", ""), b.get("name", ""), b.get("asset_type", "line"), float(b.get("capacity_mw", 0)), b.get("region", ""), b.get("parent_id"))
            elif p == ["api", "facilities"]: out = self.service.register_facility(actor, role, b.get("name", ""), b.get("facility_type", "hospital"), int(b.get("asset_id", 0)), int(b.get("priority", 1)), float(b.get("backup_power_mw", 0)))
            elif p == ["api", "outages"]: out = self.service.create_outage(actor, role, b.get("incident_code", ""), b.get("title", ""), b.get("affected_regions", []))
            elif p == ["api", "telemetry"]: out = self.service.record_telemetry(actor, role, int(b.get("asset_id", 0)), float(b.get("load_mw", 0)), float(b.get("voltage_kv", 0)), b.get("timestamp", ""))
            elif p == ["api", "plans"]: out = self.service.create_plan(actor, role, int(b.get("outage_id", 0)), b.get("steps", []))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "submit": out = self.service.submit_plan(actor, role, int(p[2]), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "approve": out = self.service.approve_plan(actor, role, int(p[2]), int(b.get("expected_revision", -1)), b.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "activate": out = self.service.activate_plan(actor, role, int(p[2]), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "change": out = self.service.make_plan_change(actor, role, int(p[2]), b.get("steps", []), int(b.get("expected_revision", -1)))
            elif p == ["api", "field-reports"]: out = self.service.field_report(actor, role, int(b.get("plan_id", 0)), int(b.get("step_no", 0)), b.get("client_report_id", ""), int(b.get("expected_plan_version", 0)), b.get("status", ""), b.get("note", ""))
            elif p == ["api", "vouchers"]: out = self.service.record_voucher(actor, role, int(b.get("plan_id", 0)), b.get("step_no", 0), b.get("phase", ""), b.get("crew", ""), b.get("asset_code", ""), b.get("field_time", ""), b.get("client_voucher_id", ""), b.get("expected_plan_version", 0), b.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "confirm": out = self.service.confirm_step(actor, role, int(p[2]), int(b.get("step_no", 0)), b.get("decision", "confirmed"), b.get("note", ""))
            elif p == ["api", "status"]: out = self.service.publish_status(actor, role, int(b.get("outage_id", 0)), int(b.get("plan_id", 0)))
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send(exc.status, {"error": exc.message})
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc: self._send(400, {"error": str(exc)})
        except Exception as exc: self._send(500, {"error": str(exc)})


def run(port: int, db_path: str, seed: bool) -> None:
    store = Store(db_path); service = GridService(store)
    if seed: service.seed()
    Handler.service = service
    print(f"grid restoration listening on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=8215); parser.add_argument("--db", default=str(DB_PATH)); parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    if args.init: Store(args.db).close()
    if args.seed or not args.init: run(args.port, args.db, args.seed)


if __name__ == "__main__": main()
