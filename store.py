"""Persistence module (存档): schema and row-level queries only.

Business judgments live in service.py / vouchers.py; HTTP entry lives in app.py.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).with_name("data.db")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message); self.status, self.message = status, message


class Store:
    def __init__(self, path: str | Path = DB_PATH):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False); self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL"); self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS assets (
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          asset_type TEXT NOT NULL, capacity_mw REAL NOT NULL, parent_id INTEGER REFERENCES assets(id), region TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS facilities (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, facility_type TEXT NOT NULL,
          asset_id INTEGER NOT NULL REFERENCES assets(id), priority INTEGER NOT NULL, backup_power_mw REAL NOT NULL,
          connected INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS outages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, incident_code TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('reported','assessing','restoring','restored')),
          affected_regions_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
          opened_by TEXT NOT NULL, opened_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plans (
          id INTEGER PRIMARY KEY AUTOINCREMENT, outage_id INTEGER NOT NULL REFERENCES outages(id),
          version INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('draft','submitted','approved','active','superseded')),
          steps_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL,
          created_at TEXT NOT NULL, approved_by TEXT, approved_at TEXT, activated_at TEXT,
          UNIQUE(outage_id,version)
        );
        CREATE TABLE IF NOT EXISTS confirmations (
          id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES plans(id), step_no INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('confirmed','blocked')), confirmed_by TEXT NOT NULL, confirmed_at TEXT NOT NULL,
          note TEXT, UNIQUE(plan_id,step_no)
        );
        CREATE TABLE IF NOT EXISTS field_reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT, client_report_id TEXT UNIQUE NOT NULL, plan_id INTEGER NOT NULL REFERENCES plans(id),
          step_no INTEGER NOT NULL, expected_plan_version INTEGER NOT NULL, status TEXT NOT NULL,
          note TEXT, merge_status TEXT NOT NULL CHECK(merge_status IN ('merged','conflict','protected')),
          conflict_reason TEXT, reported_by TEXT NOT NULL, received_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS work_vouchers (
          id INTEGER PRIMARY KEY AUTOINCREMENT, client_voucher_id TEXT UNIQUE NOT NULL,
          plan_id INTEGER NOT NULL REFERENCES plans(id), plan_version INTEGER NOT NULL, step_no INTEGER NOT NULL,
          phase TEXT NOT NULL CHECK(phase IN ('start','finish')),
          crew TEXT NOT NULL, asset_code TEXT NOT NULL, field_time TEXT NOT NULL,
          merge_status TEXT NOT NULL CHECK(merge_status IN ('merged','conflict')), conflict_reason TEXT,
          note TEXT, received_by TEXT NOT NULL, received_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telemetry (
          id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL REFERENCES assets(id), load_mw REAL NOT NULL,
          voltage_kv REAL NOT NULL, timestamp TEXT NOT NULL, valid INTEGER NOT NULL, anomaly TEXT,
          recorded_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS published_status (
          id INTEGER PRIMARY KEY AUTOINCREMENT, outage_id INTEGER NOT NULL REFERENCES outages(id),
          plan_id INTEGER NOT NULL REFERENCES plans(id), version INTEGER NOT NULL, status_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
          entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL
        );
        """)
        self.conn.commit()

    def audit(self, actor: str, action: str, entity_type: str, entity_id: object, details: dict) -> None:
        self.conn.execute("INSERT INTO audit_log(at,actor,action,entity_type,entity_id,details_json) VALUES(?,?,?,?,?,?)",
                          (now(), actor, action, entity_type, str(entity_id), j(details)))

    # -- work voucher archive (停复电凭证存档) --
    def find_voucher(self, client_voucher_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM work_vouchers WHERE client_voucher_id=?", (client_voucher_id,)).fetchone()

    def insert_voucher(self, v: dict) -> int:
        cur = self.conn.execute("""INSERT INTO work_vouchers(client_voucher_id,plan_id,plan_version,step_no,phase,crew,asset_code,
                                   field_time,merge_status,conflict_reason,note,received_by,received_at)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (v["client_voucher_id"], v["plan_id"], v["plan_version"], v["step_no"], v["phase"], v["crew"],
                                 v["asset_code"], v["field_time"], v["merge_status"], v.get("conflict_reason"), v.get("note"),
                                 v["received_by"], now()))
        return cur.lastrowid

    def get_voucher(self, voucher_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM work_vouchers WHERE id=?", (voucher_id,)).fetchone()

    def latest_voucher(self, plan_id: int, step_no: int, phase: str, merge_status: str) -> sqlite3.Row | None:
        return self.conn.execute("""SELECT * FROM work_vouchers WHERE plan_id=? AND step_no=? AND phase=? AND merge_status=?
                                    ORDER BY id DESC LIMIT 1""", (plan_id, step_no, phase, merge_status)).fetchone()

    def latest_conflict_finish(self, plan_id: int, step_no: int) -> sqlite3.Row | None:
        return self.conn.execute("""SELECT * FROM work_vouchers WHERE plan_id=? AND step_no=? AND phase='finish'
                                    AND merge_status='conflict' ORDER BY id DESC LIMIT 1""", (plan_id, step_no)).fetchone()

    def plan_has_vouchers(self, plan_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM work_vouchers WHERE plan_id=? LIMIT 1", (plan_id,)).fetchone() is not None

    def outage_has_older_versions(self, outage_id: int, version: int) -> bool:
        return self.conn.execute("SELECT 1 FROM plans WHERE outage_id=? AND version<? LIMIT 1", (outage_id, version)).fetchone() is not None

    def close(self) -> None: self.conn.close()
