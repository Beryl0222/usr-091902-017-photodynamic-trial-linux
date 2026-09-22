"""服务契约测试：健康入口 + 受控流程 HTTP 接口。"""

import datetime
import hashlib
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, SERVICE_NAME, health_payload

REG = {"energy": 100, "durationMinutes": 300}
TODAY = datetime.date.today()
TREAT_DAY = TODAY + datetime.timedelta(days=1)
EVAL_DAY = TREAT_DAY + datetime.timedelta(days=14)


def iso(day, hour=9, minute=0):
    return datetime.datetime(day.year, day.month, day.day, hour, minute).isoformat(timespec="minutes")


def checksum(seed):
    return hashlib.sha256(seed.encode()).hexdigest()


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 每个测试类共享一个全新内存存储的 Handler（通过 service.api 工厂重建）
        from trial.api import Api
        from trial.coordinator import TrialCoordinator
        from trial.store import EventStore
        cls.api = Api(TrialCoordinator(EventStore()))
        cls.handler_cls = type("BoundHandler", (Handler,), {"_api": cls.api})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler_cls)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, command, **payload):
        data = json.dumps(payload).encode("utf-8")
        request = Request(f"{self.base_url}/api/commands/{command}", data=data,
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def call_expect_error(self, command, status, **payload):
        data = json.dumps(payload).encode("utf-8")
        request = Request(f"{self.base_url}/api/commands/{command}", data=data,
                          headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, status)
        body = json.load(caught.exception)
        caught.exception.close()
        return body

    def get(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            return response.status, json.load(response)

    # ---- 基础健康契约（保持不破坏） ----
    def test_health_payload_has_stable_identity(self):
        self.assertEqual(health_payload(),
                         {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME})

    def test_health_endpoint_returns_json(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, health_payload())

    def test_unknown_route_is_not_exposed(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    # ---- 受控流程接口契约 ----
    def _bootstrapped(self):
        _, p = self.call("draft_protocol", version="V1.0",
                         drug_levels=["0.5mg/kg"], light_regimens=[REG])
        self.call("submit_protocol", protocol_id=p["id"])
        self.call("review_protocol", protocol_id=p["id"], decision="放行",
                  role="安全委员会")
        self.call("activate_protocol", protocol_id=p["id"])
        _, site = self.call("register_site", code="A01", name="一中心",
                            capabilities=["光动力介入"])
        self.call("activate_site", site_id=site["id"])
        _, drug = self.call("register_material", kind="药物", code="D-01", name="光敏剂")
        _, device = self.call("register_material", kind="器械", code="X-01",
                              name="球囊激光光纤")
        _, db = self.call("receive_batch", material_id=drug["id"], batch_no="DA",
                          expiry="2026-12-31", quantity=10)
        _, xb = self.call("receive_batch", material_id=device["id"], batch_no="XA",
                          expiry="2026-12-31", quantity=5)
        _, cohort = self.call("define_cohort", protocol_id=p["id"], name="C1",
                              level=1, drug_dose="0.5mg/kg", light_regimen=REG,
                              target_size=8)
        self.call("submit_cohort", cohort_id=cohort["id"])
        self.call("review_cohort", cohort_id=cohort["id"], decision="放行",
                  role="安全委员会")
        self.call("open_cohort", cohort_id=cohort["id"])
        return p, site, db, xb, cohort

    def test_safety_gate_blocks_unreleased_cohort_over_http(self):
        _, p = self.call("draft_protocol", version="V9.0",
                         drug_levels=["0.5mg/kg"], light_regimens=[REG])
        self.call("submit_protocol", protocol_id=p["id"])
        self.call("review_protocol", protocol_id=p["id"], decision="放行",
                  role="安全委员会")
        self.call("activate_protocol", protocol_id=p["id"])
        _, cohort = self.call("define_cohort", protocol_id=p["id"], name="C9",
                              level=1, drug_dose="0.5mg/kg", light_regimen=REG,
                              target_size=3)
        body = self.call_expect_error("open_cohort", 409, cohort_id=cohort["id"])
        self.assertEqual(body["error"], "SAFETY_GATE")

    def test_forbidden_for_non_committee_review(self):
        _, p = self.call("draft_protocol", version="V8.0",
                         drug_levels=["0.5mg/kg"], light_regimens=[REG])
        self.call("submit_protocol", protocol_id=p["id"])
        body = self.call_expect_error("review_protocol", 403, protocol_id=p["id"],
                                      decision="放行", role="研究者")
        self.assertEqual(body["error"], "FORBIDDEN")

    def test_unknown_command_and_bad_json(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(f"{self.base_url}/api/commands/nope", b"{}",
                            method="POST"), timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(f"{self.base_url}/api/commands/draft_protocol",
                            b"{not json", method="POST"), timeout=2)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_full_flow_with_window_and_blinded_view(self):
        p, site, db, xb, cohort = self._bootstrapped()
        _, s = self.call("register_subject", site_id=site["id"], code="SB-1")
        self.call("sign_consent", subject_id=s["id"], protocol_id=p["id"],
                  doc_ref="icf://deid/1", doc_checksum=checksum("icf"))
        self.call("record_eligibility", subject_id=s["id"],
                  inclusion_results={"I1": True}, exclusion_results={"E1": False})
        self.call("enroll_subject", subject_id=s["id"])
        self.call("assign_cohort", subject_id=s["id"], cohort_id=cohort["id"],
                  drug_batch_id=db["id"], device_batch_id=xb["id"])
        # 错误照光参数被阻止
        body = self.call_expect_error(
            "illuminate", 409, subject_id=s["id"], at=iso(TREAT_DAY, 9, 30),
            energy=150, duration_minutes=420)
        self.assertEqual(body["error"], "CONFLICT")
        self.call("administer_drug", subject_id=s["id"], at=iso(TREAT_DAY, 9, 0))
        self.call("illuminate", subject_id=s["id"], at=iso(TREAT_DAY, 9, 30),
                  energy=100, duration_minutes=300)
        self.call("finish_treatment", subject_id=s["id"], at=iso(TREAT_DAY, 10, 0))
        # 越窗材料拒收（窗口第14天±2，提前3天必在窗外）
        outside = (EVAL_DAY - datetime.timedelta(days=3)).isoformat()
        body = self.call_expect_error(
            "submit_material", 409, subject_id=s["id"], kind="影像",
            deidentified_ref="img://1", checksum=checksum("img"),
            collected_at=outside)
        self.assertEqual(body["error"], "WINDOW_VIOLATION")
        for kind, seed in (("影像", "img"), ("病理", "path")):
            self.call("submit_material", subject_id=s["id"], kind=kind,
                      deidentified_ref=f"{kind}://deid/1",
                      checksum=checksum(seed), collected_at=EVAL_DAY.isoformat())
            self.call("verify_checksum", subject_id=s["id"], kind=kind,
                      checksum=checksum(seed))
        _, eval_result = self.call("evaluability", subject_id=s["id"])
        self.assertEqual(eval_result[0]["status"], "可评估")
        _, outcome = self.call("record_outcome", subject_id=s["id"],
                               result="可切除", decision="R0 切除",
                               decided_by="外科主任", at=iso(EVAL_DAY, 15, 0))
        self.assertEqual(outcome["provenance"]["protocolVersion"], "V1.0")
        # 盲态视图：GET 带 role=盲态评价者
        _, blinded = self.get(f"/api/subjects/{s['id']}?role={quote('盲态评价者')}")
        self.assertTrue(blinded["blinded"])
        self.assertIsNone(blinded["assignment"])
        self.assertIsNone(blinded["protocol"])
        self.assertEqual(len(blinded["evaluations"]), 2)
        # 非盲视图包含完整溯源
        _, full = self.get(f"/api/subjects/{s['id']}?role={quote('研究者')}")
        self.assertFalse(full["blinded"])
        self.assertEqual(full["assignment"]["drugBatchNo"], "DA")
        self.assertEqual(full["outcome"]["medicalDecision"], "R0 切除")


if __name__ == "__main__":
    unittest.main()
