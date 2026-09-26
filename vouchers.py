"""Voucher judgment module (判定): 停复电开工/完工凭证的接收、生效与卡住原因判定。

规则：
- 开工、完工凭证均记录班组、设备序列、现场时刻。
- 同一设备在同一计划版本内，上一段未解除（无有效完工凭证）时，下一段完工冲突，不能计入生效。
- 同一 client_voucher_id 重复回传沿用首条记录（幂等）。
- 仅 active 且版本匹配的计划下、merge_status='merged' 的凭证有效；计划改版后旧凭证不再生效。
"""
from __future__ import annotations

import json
from datetime import datetime

from store import ApiError, Store, j, now

PHASE_START, PHASE_FINISH = "start", "finish"


def parse_field_time(value: object) -> str:
    text = str(value or "").strip()
    if not text: raise ApiError(400, "现场时刻不能为空")
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "现场时刻不是合法 ISO 8601 时间") from exc
    return text


class VoucherBook:
    def __init__(self, store: Store):
        self.store, self.conn = store, store.conn

    @staticmethod
    def dict_of(row) -> dict:
        return {"id": row["id"], "client_voucher_id": row["client_voucher_id"], "plan_id": row["plan_id"],
                "plan_version": row["plan_version"], "step_no": row["step_no"], "phase": row["phase"],
                "crew": row["crew"], "asset_code": row["asset_code"], "field_time": row["field_time"],
                "merge_status": row["merge_status"], "conflict_reason": row["conflict_reason"],
                "note": row["note"], "received_by": row["received_by"], "received_at": row["received_at"]}

    def receive(self, actor: str, plan_id: int, step_no: int, phase: str, crew: str, asset_code: str,
                field_time: str, client_voucher_id: str, expected_plan_version: int, note: str = "") -> dict:
        if not client_voucher_id or not str(client_voucher_id).strip(): raise ApiError(400, "客户端凭证编号不能为空")
        if phase not in (PHASE_START, PHASE_FINISH): raise ApiError(400, "凭证阶段不合法，必须是 start 或 finish")
        if not crew or not str(crew).strip(): raise ApiError(400, "班组不能为空")
        field_time = parse_field_time(field_time)

        plan = self.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan: raise ApiError(404, "计划不存在")
        steps = json.loads(plan["steps_json"])
        steps_by_seq = {int(x["seq"]): x for x in steps}
        if step_no not in steps_by_seq: raise ApiError(400, "计划中没有该步骤")
        step = steps_by_seq[step_no]
        if asset_code != step["asset"]: raise ApiError(409, f"设备序列与步骤 {step_no} 不符，应为 {step['asset']}")

        # 重复回传沿用首条记录
        duplicate = self.store.find_voucher(client_voucher_id)
        if duplicate: return self.dict_of(duplicate)

        merge_status, conflict = "merged", None
        if plan["state"] != "active":
            merge_status, conflict = "conflict", "计划尚未激活，凭证暂不生效"
        elif int(expected_plan_version) != int(plan["version"]):
            merge_status, conflict = "conflict", "凭证基于旧计划版本，计划改版后旧凭证失效"
        elif phase == PHASE_FINISH:
            blocked_by = self._asset_busy_step(plan, steps_by_seq, step_no, step["asset"])
            if blocked_by is not None:
                merge_status, conflict = "conflict", f"同一设备 {step['asset']} 的上一段 {blocked_by} 尚未解除完工，不能进行本段完工"

        record = {"client_voucher_id": str(client_voucher_id).strip(), "plan_id": plan_id, "plan_version": plan["version"],
                  "step_no": step_no, "phase": phase, "crew": str(crew).strip(), "asset_code": asset_code,
                  "field_time": field_time, "merge_status": merge_status, "conflict_reason": conflict, "note": note,
                  "received_by": actor}
        with self.conn:
            voucher_id = self.store.insert_voucher(record)
            self.store.audit(actor, f"voucher.{phase}", "plan", plan_id,
                             {"step_no": step_no, "asset": asset_code, "crew": record["crew"],
                              "field_time": field_time, "merge_status": merge_status, "conflict": conflict,
                              "client_voucher_id": record["client_voucher_id"]})
        return self.dict_of(self.store.get_voucher(voucher_id))

    def _asset_busy_step(self, plan, steps_by_seq: dict, step_no: int, asset_code: str) -> int | None:
        """同设备序号更早的步骤若无有效完工凭证，则该设备尚未解除，返回卡住的步骤号。"""
        for other in sorted(steps_by_seq):
            if other >= step_no: break
            if steps_by_seq[other]["asset"] == asset_code:
                if not self.store.latest_voucher(plan["id"], other, PHASE_FINISH, "merged"):
                    return other
        return None

    def missing_finish_steps(self, plan_id: int, steps: list[dict]) -> list[int]:
        return [int(step["seq"]) for step in steps
                if not self.store.latest_voucher(plan_id, int(step["seq"]), PHASE_FINISH, "merged")]

    def step_views(self, plan, steps: list[dict]) -> list[dict]:
        """每步凭证与卡住原因的只读视图（供详情页与发布判定共用）。"""
        plan_id = plan["id"]
        views = []
        for raw in steps:
            seq = int(raw["seq"])
            start = self.store.latest_voucher(plan_id, seq, PHASE_START, "merged")
            finish = self.store.latest_voucher(plan_id, seq, PHASE_FINISH, "merged")
            rejected = self.store.latest_conflict_finish(plan_id, seq)
            reasons: list[str] = []
            if start is None:
                reasons.append("尚无有效开工凭证（班组、设备序列、现场时刻缺失）")
            if finish is None:
                if rejected is not None and rejected["conflict_reason"]:
                    reasons.append(rejected["conflict_reason"])
                else:
                    reasons.append("尚无有效完工凭证")
            if (start is not None or finish is not None or rejected is not None):
                if plan["state"] == "superseded":
                    reasons.append("计划已改版，本版本停复电凭证全部失效，需按当前版本重新回传")
                elif plan["state"] in ("draft", "submitted", "approved"):
                    reasons.append("计划尚未激活，凭证暂不生效")
            views.append({"step_no": seq, "action": raw.get("action", ""), "asset": raw["asset"],
                          "start_voucher": self.dict_of(start) if start else None,
                          "finish_voucher": self.dict_of(finish) if finish else None,
                          "finish_effective": finish is not None and plan["state"] == "active",
                          "block_reasons": reasons})
        return views
