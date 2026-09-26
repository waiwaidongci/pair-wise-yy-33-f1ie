"""停复电凭证判定模块（policy）。

规则：
- 开工/完工凭证都要在当前激活计划上回传，计划改版（旧计划 superseded）后旧凭证失效。
- 完工前该步骤必须已有有效开工凭证，且现场完工时刻不得早于开工时刻。
- 同一设备（asset_code）在上一段作业未完工解除前，不允许下一段完工（按现场时刻取
  上一段有效开工）。
- 重复回传由存档层按 client_voucher_id 去重，沿用首条记录。
- 发布“恢复完成”时，当前激活计划的每一步都必须持有当前版本的有效完工凭证。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime


def _parse_field_time(value: str) -> datetime | None:
    """解析现场时刻；仅接受带时区的 ISO 8601 字符串。"""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


class VoucherPolicy:
    def __init__(self, archive):
        self.archive = archive

    # ---------- 单条凭证受理判定 ----------
    def assess(self, *, plan: sqlite3.Row, steps: list[dict], step_no: int, kind: str, crew: str,
               asset_code: str, field_time: str) -> tuple[bool, str | None]:
        """返回 (是否有效, 拒收原因)。受理但无效的凭证仍会留档。"""
        if not str(crew or "").strip():
            return False, "缺少作业班组"
        if plan["state"] == "superseded":
            return False, "计划已改版，旧版本凭证失效"
        if plan["state"] != "active":
            return False, "计划尚未激活"
        step = next((s for s in steps if int(s["seq"]) == int(step_no)), None)
        if step is None:
            return False, "计划中没有该步骤"
        if str(asset_code or "").strip() != str(step["asset"]):
            return False, f"设备序列与计划不符：该步骤设备为 {step['asset']}"
        started_at = _parse_field_time(field_time)
        if started_at is None:
            return False, "现场时刻不合法（需带时区的 ISO 8601）"
        if kind == "complete":
            start = self.archive.valid_start(plan["id"], step_no)
            if start is None:
                return False, "该步骤尚无有效开工凭证，不能完工"
            if started_at < _parse_field_time(start["field_time"]):
                return False, "完工现场时刻早于开工现场时刻"
            blocking = self._unreleased_predecessor(plan["id"], steps, step, started_at)
            if blocking is not None:
                return False, f"同设备 {step['asset']} 上一段步骤 {blocking} 尚未完工解除"
        return True, None

    def _unreleased_predecessor(self, plan_id: int, steps: list[dict], current: dict,
                                complete_time: datetime) -> int | None:
        """找同一设备上、现场时间在先、仍未有效完工的最近一段步骤。"""
        candidates = []
        for other in steps:
            if int(other["seq"]) >= int(current["seq"]) or other["asset"] != current["asset"]:
                continue
            start = self.archive.valid_start(plan_id, int(other["seq"]))
            if start is None:
                continue
            if _parse_field_time(start["field_time"]) <= complete_time:
                candidates.append(other)
        for other in sorted(candidates, key=lambda s: int(s["seq"]), reverse=True):
            complete = self.archive.valid_complete(plan_id, int(other["seq"]))
            if complete is None:
                return int(other["seq"])
        return None

    # ---------- 每步凭证视图与卡住原因 ----------
    def step_view(self, *, plan: sqlite3.Row, steps: list[dict]) -> list[dict]:
        if plan["state"] == "superseded":
            return [{"step_no": int(s["seq"]), "asset": s["asset"], "status": "stale",
                     "stuck_reason": "计划已改版，旧版本凭证失效"} for s in steps]
        if plan["state"] != "active":
            return [{"step_no": int(s["seq"]), "asset": s["asset"], "status": "inactive",
                     "stuck_reason": "计划尚未激活"} for s in steps]
        view = []
        for step in steps:
            no = int(step["seq"])
            start, complete = self.archive.valid_start(plan["id"], no), self.archive.valid_complete(plan["id"], no)
            if complete is not None:
                status, stuck = "complete", None
            elif start is None:
                status, stuck = "awaiting_start", "等待现场开工凭证"
            else:
                status, stuck = "awaiting_complete", "设备尚未解除（等待有效完工凭证）"
            invalid = self.archive.latest_invalid(plan["id"], no)
            if invalid is not None and (complete is None or invalid["id"] > complete["id"]):
                stuck = (stuck + "；" if stuck else "") + f"最近回传被拒收：{invalid['reject_reason']}"
            view.append({"step_no": no, "asset": step["asset"], "status": status, "stuck_reason": stuck,
                         "start": _brief(start), "complete": _brief(complete)})
        return view

    # ---------- 发布闸门 ----------
    def publish_gate(self, plan: sqlite3.Row, steps: list[dict]) -> list[str]:
        """返回阻碍“恢复完成”发布的原因列表；空列表表示放行。"""
        if plan["state"] == "superseded":
            return ["当前计划已被新版本取代，旧凭证失效"]
        if plan["state"] != "active":
            return ["计划尚未激活"]
        return [f"步骤 {item['step_no']}（{item['asset']}）：{item['stuck_reason']}"
                for item in self.step_view(plan=plan, steps=steps) if item["status"] != "complete"]


def _brief(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {"id": row["id"], "kind": row["kind"], "crew": row["crew"], "asset_code": row["asset_code"],
            "field_time": row["field_time"], "plan_version": row["plan_version"]}
