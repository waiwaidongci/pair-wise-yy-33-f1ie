"""停复电凭证存档模块（append-only archive）。

只负责凭证的落库与查询，不做业务判定：是否有效由 voucher_policy.VoucherPolicy
判定后以 valid/reject_reason 形式随记录归档。同一 client_voucher_id 的重复回传
沿用首条记录（唯一约束 + 调用方去重）。
"""
from __future__ import annotations

import sqlite3


class VoucherArchive:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def install(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS vouchers (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          client_voucher_id TEXT UNIQUE NOT NULL,
          plan_id INTEGER NOT NULL REFERENCES plans(id),
          plan_version INTEGER NOT NULL,
          step_no INTEGER NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('start','complete')),
          crew TEXT NOT NULL,
          asset_code TEXT NOT NULL,
          field_time TEXT NOT NULL,
          valid INTEGER NOT NULL,
          reject_reason TEXT,
          note TEXT,
          reported_by TEXT NOT NULL,
          received_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vouchers_plan_step ON vouchers(plan_id, step_no);
        """)

    def add(self, *, client_voucher_id: str, plan_id: int, plan_version: int, step_no: int, kind: str,
            crew: str, asset_code: str, field_time: str, valid: bool, reject_reason: str | None,
            note: str, reported_by: str, received_at: str) -> int:
        cur = self.conn.execute("""INSERT INTO vouchers(client_voucher_id,plan_id,plan_version,step_no,kind,crew,
                                    asset_code,field_time,valid,reject_reason,note,reported_by,received_at)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (client_voucher_id, plan_id, plan_version, step_no, kind, crew, asset_code,
                                 field_time, int(valid), reject_reason, note, reported_by, received_at))
        return int(cur.lastrowid)

    def get_by_client(self, client_voucher_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM vouchers WHERE client_voucher_id=?", (client_voucher_id,)).fetchone()

    def list_for_plan(self, plan_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM vouchers WHERE plan_id=? ORDER BY id", (plan_id,)))

    def valid_start(self, plan_id: int, step_no: int) -> sqlite3.Row | None:
        return self.conn.execute("""SELECT * FROM vouchers WHERE plan_id=? AND step_no=? AND kind='start' AND valid=1
                                    ORDER BY id LIMIT 1""", (plan_id, step_no)).fetchone()

    def valid_complete(self, plan_id: int, step_no: int) -> sqlite3.Row | None:
        return self.conn.execute("""SELECT * FROM vouchers WHERE plan_id=? AND step_no=? AND kind='complete' AND valid=1
                                    ORDER BY id LIMIT 1""", (plan_id, step_no)).fetchone()

    def latest_invalid(self, plan_id: int, step_no: int) -> sqlite3.Row | None:
        return self.conn.execute("""SELECT * FROM vouchers WHERE plan_id=? AND step_no=? AND valid=0
                                    ORDER BY id DESC LIMIT 1""", (plan_id, step_no)).fetchone()
