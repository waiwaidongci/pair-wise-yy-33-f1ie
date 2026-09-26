import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store


class GridFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = GridService(Store(Path(self.tmp.name) / "g.db"))
        self.sub = self.s.register_asset("dispatcher", "dispatcher", "SUB", "中心站", "substation", 200, "A")
        self.line = self.s.register_asset("dispatcher", "dispatcher", "LINE", "线路", "line", 100, "A", self.sub["id"])
        self.s.register_facility("dispatcher", "dispatcher", "医院", "hospital", self.sub["id"], 1, 50)

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def plan(self, code="OUT-1"):
        outage = self.s.create_outage("dispatcher", "dispatcher", code, "线路跳闸", ["A"])
        plan = self.s.create_plan("dispatcher", "dispatcher", outage["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"], "安全校核通过")
        return outage, self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])

    def test_full_restore_offline_merge_duplicate_and_plan_change(self):
        outage, plan = self.plan()
        report = self.s.field_report("field", "field", plan["id"], 1, "client-1", plan["version"], "completed", "设备已检查")
        self.assertEqual("merged", report["merge_status"])
        self.s.submit_voucher("field", "field", plan["id"], 1, "v-s1", "start", "甲班", "SUB", "2026-09-26T08:00:00Z")
        self.s.submit_voucher("field", "field", plan["id"], 1, "v-c1", "complete", "甲班", "SUB", "2026-09-26T09:00:00Z")
        confirmed = self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed", "现场照片核验")
        self.assertEqual("confirmed", confirmed["status"])
        protected = self.s.field_report("field", "field", plan["id"], 1, "client-1-protected", plan["version"], "blocked", "补充遥测")
        self.assertEqual("protected", protected["merge_status"])
        plan2 = self.s.make_plan_change("dispatcher", "dispatcher", plan["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}], plan["revision"])
        detail = self.s.plan_detail(plan2["id"])
        self.assertEqual(1, len(detail["confirmations"]))
        plan2 = self.s.submit_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.approve_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.activate_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        # 改版后旧凭证失效，新版本每步都要重新回传有效开工/完工凭证
        self.s.field_report("field", "field", plan2["id"], 2, "client-2", plan2["version"], "completed", "已送电")
        self.s.submit_voucher("field", "field", plan2["id"], 1, "v2-s1", "start", "甲班", "SUB", "2026-09-26T10:00:00Z")
        self.s.submit_voucher("field", "field", plan2["id"], 1, "v2-c1", "complete", "甲班", "SUB", "2026-09-26T10:30:00Z")
        self.s.submit_voucher("field", "field", plan2["id"], 2, "v2-s2", "start", "乙班", "LINE", "2026-09-26T11:00:00Z")
        self.s.submit_voucher("field", "field", plan2["id"], 2, "v2-c2", "complete", "乙班", "LINE", "2026-09-26T11:30:00Z")
        self.s.confirm_step("dispatcher", "dispatcher", plan2["id"], 2, "confirmed")
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan2["id"])
        self.assertEqual("restored", status["status"]["state"])

    def test_anomaly_stale_report_dependency_and_permissions(self):
        outage, plan = self.plan("OUT-2")
        anomaly = self.s.record_telemetry("operator", "operator", self.line["id"], 500, 220, "2026-09-24T00:00:00Z")
        self.assertFalse(anomaly["valid"])
        with self.assertRaises(ApiError):
            self.s.field_report("operator", "operator", plan["id"], 1, "bad-role", plan["version"], "completed")
        stale = self.s.field_report("field", "field", plan["id"], 2, "stale", plan["version"] - 1, "completed")
        self.assertEqual("conflict", stale["merge_status"])
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        with self.assertRaises(ApiError):
            self.s.create_plan("dispatcher", "dispatcher", outage["id"], [{"seq": 1, "action": "送电", "asset": "LINE", "required_mw": 101}])


if __name__ == "__main__": unittest.main()
