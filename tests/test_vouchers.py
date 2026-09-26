import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store


class VoucherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = GridService(Store(Path(self.tmp.name) / "g.db"))
        self.sub = self.s.register_asset("dispatcher", "dispatcher", "SUB", "中心站", "substation", 200, "A")
        self.line = self.s.register_asset("dispatcher", "dispatcher", "LINE", "线路", "line", 100, "A", self.sub["id"])
        outage = self.s.create_outage("dispatcher", "dispatcher", "OUT-V", "线路跳闸", ["A"])
        plan = self.s.create_plan("dispatcher", "dispatcher", outage["id"], [
            {"seq": 1, "action": "停电检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "转供", "asset": "SUB", "required_mw": 60, "depends_on": [1], "critical": True},
            {"seq": 3, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [2], "critical": True}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"], "安全校核通过")
        self.outage, self.plan = outage, self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def voucher(self, step, phase, cid, crew="甲班", asset=None, t="2026-09-26T10:00:00Z", version=None):
        return self.s.record_voucher("field", "field", self.plan["id"], step, phase, crew, asset or ("SUB" if step < 3 else "LINE"),
                                     t, cid, version if version is not None else self.plan["version"])

    def test_voucher_requires_role_and_fields(self):
        with self.assertRaises(ApiError):
            self.s.record_voucher("operator", "operator", self.plan["id"], 1, "start", "甲班", "SUB", "2026-09-26T10:00:00Z", "x", self.plan["version"])
        with self.assertRaises(ApiError):
            self.voucher(1, "start", "no-crew", crew=" ")
        with self.assertRaises(ApiError):
            self.voucher(1, "start", "bad-time", t="not-a-time")
        with self.assertRaises(ApiError):
            self.s.record_voucher("field", "field", self.plan["id"], 1, "finish", "甲班", "LINE",
                                  "2026-09-26T10:00:00Z", "wrong-asset", self.plan["version"])

    def test_duplicate_voucher_keeps_first_record(self):
        first = self.voucher(1, "start", "dup-1", crew="甲班", t="2026-09-26T10:00:00Z")
        again = self.voucher(1, "finish", "dup-1", crew="乙班", t="2026-09-26T11:00:00Z")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual("start", again["phase"])
        self.assertEqual("甲班", again["crew"])
        self.assertEqual("2026-09-26T10:00:00Z", again["field_time"])

    def test_same_asset_next_step_finish_blocked_until_previous_released(self):
        self.voucher(1, "start", "s1-start", t="2026-09-26T08:00:00Z")
        # SUB 上的步骤 1 尚未完工解除，步骤 2（同一设备）不许完工
        blocked = self.voucher(2, "finish", "s2-finish-blocked", t="2026-09-26T08:30:00Z")
        self.assertEqual("conflict", blocked["merge_status"])
        self.assertIn("尚未解除", blocked["conflict_reason"])
        # 不同设备不受影响：但 LINE 步骤 3 依赖步骤 2 的确认，凭证本身可记录
        self.assertEqual("merged", self.voucher(3, "start", "s3-start", t="2026-09-26T08:31:00Z")["merge_status"])
        # 上一段解除后，下一段完工才生效
        self.assertEqual("merged", self.voucher(1, "finish", "s1-finish", t="2026-09-26T08:40:00Z")["merge_status"])
        self.voucher(2, "start", "s2-start", t="2026-09-26T08:45:00Z")
        ok = self.voucher(2, "finish", "s2-finish-ok", t="2026-09-26T09:00:00Z")
        self.assertEqual("merged", ok["merge_status"])
        detail = self.s.plan_vouchers(self.plan["id"])
        by_no = {v["step_no"]: v for v in detail["vouchers"]}
        self.assertTrue(by_no[1]["finish_effective"])
        self.assertTrue(by_no[2]["finish_effective"])
        self.assertFalse(by_no[3]["finish_effective"])

    def test_publish_requires_every_current_finish_voucher(self):
        self.voucher(1, "start", "s1-start"); self.voucher(1, "finish", "s1-finish")
        published = self.s.publish_status("dispatcher", "dispatcher", self.outage["id"], self.plan["id"])
        self.assertEqual("restoring", published["status"]["state"])
        self.assertFalse(published["status"]["voucher_complete"])
        self.assertEqual([2, 3], published["status"]["steps_missing_finish_voucher"])

    def test_plan_change_invalidates_old_vouchers_and_blocks_publish(self):
        self.voucher(1, "start", "old-s1-start"); self.voucher(1, "finish", "old-s1-finish")
        plan2 = self.s.make_plan_change("dispatcher", "dispatcher", self.plan["id"], [
            {"seq": 1, "action": "停电检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "转供", "asset": "SUB", "required_mw": 60, "depends_on": [1], "critical": True},
            {"seq": 3, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [2], "critical": True}],
            self.plan["revision"])
        # 旧版本计划上的凭证被标注为失效
        old_views = self.s.plan_vouchers(self.plan["id"])["vouchers"]
        self.assertTrue(any("改版" in r for v in old_views for r in v["block_reasons"]))
        plan2 = self.s.submit_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.approve_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.activate_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        # 回传旧版本号 -> 冲突，不生效
        stale = self.s.record_voucher("field", "field", plan2["id"], 1, "finish", "甲班", "SUB",
                                      "2026-09-26T12:00:00Z", "stale-finish", self.plan["version"])
        self.assertEqual("conflict", stale["merge_status"])
        missing = self.s.publish_status("dispatcher", "dispatcher", self.outage["id"], plan2["id"])["status"]
        self.assertEqual("restoring", missing["state"])
        self.assertFalse(missing["voucher_complete"])
        # 新版本每步补齐完工凭证（含同设备顺序）后方可发布恢复完成
        for step, cid, asset, t in [(1, "n1", "SUB", "2026-09-26T12:10:00Z"),
                                    (2, "n2", "SUB", "2026-09-26T12:20:00Z"),
                                    (3, "n3", "LINE", "2026-09-26T12:30:00Z")]:
            self.s.record_voucher("field", "field", plan2["id"], step, "start", f"{cid}班", asset, t, f"{cid}-s", plan2["version"])
            self.s.record_voucher("field", "field", plan2["id"], step, "finish", f"{cid}班", asset, t, f"{cid}-f", plan2["version"])
        for step, report in [(1, "r1"), (2, "r2"), (3, "r3")]:
            self.s.field_report("field", "field", plan2["id"], step, report, plan2["version"], "completed")
            self.s.confirm_step("dispatcher", "dispatcher", plan2["id"], step, "confirmed")
        done = self.s.publish_status("dispatcher", "dispatcher", self.outage["id"], plan2["id"])["status"]
        self.assertEqual("restored", done["state"])
        self.assertTrue(done["voucher_complete"])


if __name__ == "__main__": unittest.main()
