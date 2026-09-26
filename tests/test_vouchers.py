import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store


class VoucherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = GridService(Store(Path(self.tmp.name) / "g.db"))
        self.s.register_asset("dispatcher", "dispatcher", "SUB", "中心站", "substation", 200, "A")
        self.s.register_asset("dispatcher", "dispatcher", "LINE", "线路", "line", 100, "A")
        outage = self.s.create_outage("dispatcher", "dispatcher", "OUT-V", "抢修", ["A"])
        plan = self.s.create_plan("dispatcher", "dispatcher", outage["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80},
            {"seq": 2, "action": "复电1", "asset": "LINE", "required_mw": 70, "depends_on": [1]},
            {"seq": 3, "action": "复电2", "asset": "LINE", "required_mw": 60, "depends_on": [2]}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"], "ok")
        self.outage, self.plan = outage, self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def _start(self, step, cid, crew, asset, t):
        return self.s.submit_voucher("field", "field", self.plan["id"], step, cid, "start", crew, asset, t)

    def _complete(self, step, cid, crew, asset, t):
        return self.s.submit_voucher("field", "field", self.plan["id"], step, cid, "complete", crew, asset, t)

    def test_records_crew_asset_field_time(self):
        v = self._start(1, "c1", "甲班", "SUB", "2026-09-26T08:00:00Z")
        self.assertTrue(v["valid"]); self.assertIsNone(v["reject_reason"])
        self.assertEqual(("甲班", "SUB", "2026-09-26T08:00:00Z"), (v["crew"], v["asset_code"], v["field_time"]))
        view = {x["step_no"]: x for x in self.s.plan_detail(self.plan["id"])["step_vouchers"]}
        self.assertEqual("awaiting_complete", view[1]["status"])
        self.assertEqual("甲班", view[1]["start"]["crew"])

    def test_complete_requires_start(self):
        v = self._complete(1, "c2", "甲班", "SUB", "2026-09-26T09:00:00Z")
        self.assertFalse(v["valid"]); self.assertIn("开工凭证", v["reject_reason"])

    def test_complete_time_before_start_rejected(self):
        self._start(1, "c3", "甲班", "SUB", "2026-09-26T10:00:00Z")
        v = self._complete(1, "c4", "甲班", "SUB", "2026-09-26T09:00:00Z")
        self.assertFalse(v["valid"]); self.assertIn("完工现场时刻早于", v["reject_reason"])

    def test_same_asset_predecessor_must_be_released(self):
        self._start(1, "s1", "甲班", "SUB", "2026-09-26T08:00:00Z")
        self._complete(1, "c1", "甲班", "SUB", "2026-09-26T09:00:00Z")
        # 步骤2 是 LINE 上第一段作业，可完工
        self._start(2, "s2", "乙班", "LINE", "2026-09-26T09:30:00Z")
        self._complete(2, "c2", "乙班", "LINE", "2026-09-26T10:00:00Z")
        # 步骤3 同属 LINE；步骤2 已解除，先开工后完工应通过
        self._start(3, "s3", "丙班", "LINE", "2026-09-26T10:30:00Z")
        ok = self._complete(3, "c3", "丙班", "LINE", "2026-09-26T11:00:00Z")
        self.assertTrue(ok["valid"])
        # 再造一个未解除场景：新计划上步骤2开工后，步骤3完工必须被拦
        plan2 = self.s.make_plan_change("dispatcher", "dispatcher", self.plan["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80},
            {"seq": 2, "action": "复电1", "asset": "LINE", "required_mw": 70, "depends_on": [1]},
            {"seq": 3, "action": "复电2", "asset": "LINE", "required_mw": 60, "depends_on": [2]}],
            self.plan["revision"])
        plan2 = self.s.submit_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.approve_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.activate_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        self._start_p(plan2["id"], 2, "d-s2", "乙班", "LINE", "2026-09-26T12:00:00Z")
        self._start_p(plan2["id"], 3, "d-s3", "丙班", "LINE", "2026-09-26T12:30:00Z")
        blocked = self._complete_p(plan2["id"], 3, "d-c3", "丙班", "LINE", "2026-09-26T13:00:00Z")
        self.assertFalse(blocked["valid"]); self.assertIn("上一段步骤 2", blocked["reject_reason"])

    def _start_p(self, pid, step, cid, crew, asset, t):
        return self.s.submit_voucher("field", "field", pid, step, cid, "start", crew, asset, t)

    def _complete_p(self, pid, step, cid, crew, asset, t):
        return self.s.submit_voucher("field", "field", pid, step, cid, "complete", crew, asset, t)

    def test_duplicate_reuses_first_record(self):
        first = self._start(1, "dup", "甲班", "SUB", "2026-09-26T08:00:00Z")
        # 同样的编号即使内容不同（且自身会被判定无效）也沿用首条
        again = self.s.submit_voucher("field", "field", self.plan["id"], 1, "dup", "complete", "错班", "WRONG", "bad-time")
        self.assertTrue(again["duplicate"]); self.assertEqual(first["id"], again["id"]); self.assertTrue(again["valid"])

    def test_wrong_asset_and_bad_time(self):
        bad_asset = self._start(1, "a1", "甲班", "LINE", "2026-09-26T08:00:00Z")
        self.assertFalse(bad_asset["valid"]); self.assertIn("设备序列", bad_asset["reject_reason"])
        bad_time = self._start(1, "a2", "甲班", "SUB", "2026-09-26 08:00")
        self.assertFalse(bad_time["valid"]); self.assertIn("现场时刻", bad_time["reject_reason"])
        with self.assertRaises(ApiError):
            self.s.submit_voucher("operator", "operator", self.plan["id"], 1, "a3", "start", "甲班", "SUB", "2026-09-26T08:00:00Z")

    def test_plan_change_invalidates_old_vouchers_and_publish_gate(self):
        self._start(1, "s1", "甲班", "SUB", "2026-09-26T08:00:00Z")
        self._complete(1, "c1", "甲班", "SUB", "2026-09-26T09:00:00Z")
        self._start(2, "s2", "乙班", "LINE", "2026-09-26T09:30:00Z")
        self._complete(2, "c2", "乙班", "LINE", "2026-09-26T10:00:00Z")
        self._start(3, "s3", "丙班", "LINE", "2026-09-26T10:30:00Z")
        self._complete(3, "c3", "丙班", "LINE", "2026-09-26T11:00:00Z")
        for no in (1, 2, 3):
            self.s.field_report("field", "field", self.plan["id"], no, f"fr{no}", self.plan["version"], "completed")
            self.s.confirm_step("dispatcher", "dispatcher", self.plan["id"], no, "confirmed")
        self.assertEqual("restored", self.s.publish_status("dispatcher", "dispatcher", self.outage["id"], self.plan["id"])["status"]["state"])

        plan2 = self.s.make_plan_change("dispatcher", "dispatcher", self.plan["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80},
            {"seq": 2, "action": "复电1", "asset": "LINE", "required_mw": 70, "depends_on": [1]},
            {"seq": 3, "action": "复电2", "asset": "LINE", "required_mw": 60, "depends_on": [2]}],
            self.plan["revision"])
        detail = self.s.plan_detail(plan2["id"])
        # 新版草稿尚未激活，新版没有任何凭证；旧计划凭证随 superseded 失效（stale）
        self.assertTrue(all(x["status"] == "inactive" for x in detail["step_vouchers"]))
        old_detail = self.s.plan_detail(self.plan["id"])
        self.assertTrue(all(x["status"] == "stale" for x in old_detail["step_vouchers"]))
        # 旧凭证对旧计划不再满足发布闸门
        self.assertTrue(any("失效" in b for b in old_detail["publish_blockers"]))
        plan2 = self.s.submit_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.approve_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.activate_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        # 确认已随版本携带，但缺当前版本凭证 → 发布闸门拒绝并给出每步原因
        with self.assertRaises(ApiError) as ctx:
            self.s.publish_status("dispatcher", "dispatcher", self.outage["id"], plan2["id"])
        self.assertIn("有效完工凭证", ctx.exception.message)
        blockers = self.s.plan_detail(plan2["id"])["publish_blockers"]
        self.assertEqual(3, len(blockers))

        for no, crew, asset, t0, t1 in [(1, "甲班", "SUB", "12:00", "12:15"),
                                        (2, "乙班", "LINE", "12:30", "12:45"),
                                        (3, "丙班", "LINE", "13:00", "13:15")]:
            self._start_p(plan2["id"], no, f"n-s{no}", crew, asset, f"2026-09-26T{t0}:00Z")
            self._complete_p(plan2["id"], no, f"n-c{no}", crew, asset, f"2026-09-26T{t1}:00Z")
        detail = self.s.plan_detail(plan2["id"])
        self.assertEqual([], detail["publish_blockers"])
        self.assertTrue(all(x["status"] == "complete" for x in detail["step_vouchers"]))
        self.assertEqual("restored", self.s.publish_status("dispatcher", "dispatcher", self.outage["id"], plan2["id"])["status"]["state"])


if __name__ == "__main__": unittest.main()
