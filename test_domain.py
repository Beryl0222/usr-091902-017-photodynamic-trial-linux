"""受控流程领域规则单元测试。"""

import datetime
import unittest

from trial import TrialCoordinator, TrialError, TrialErrorCode
from trial.store import EventStore, fixed_clock
from trial.catalog import observation_window

C = "安全委员会"
INV = "研究者"
COORD = "试验协调员"
BLIND = "盲态评价者"

REG_R100 = {"energy": 100, "durationMinutes": 300}
REG_R150 = {"energy": 150, "durationMinutes": 420}


def checksum(seed):
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()


def expect_error(test, code, func, *args, **kwargs):
    with test.assertRaises(TrialError) as caught:
        func(*args, **kwargs)
    test.assertEqual(caught.exception.code, code, caught.exception.message)
    return caught.exception


class FlowTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = fixed_clock("2026-01-05T08:00:00")
        self.coordinator = TrialCoordinator(EventStore(self.clock))
        self.c = self.coordinator

    def activate_protocol(self, version="V1.0", levels=None, regimens=None):
        p = self.c.draft_protocol(version, levels or ["0.5mg/kg", "1.0mg/kg"],
                                  regimens or [REG_R100, REG_R150], "初版")
        self.c.submit_protocol(p["id"])
        self.c.safety_review_protocol(p["id"], "放行", actor=C)
        return self.c.activate_protocol(p["id"])

    def active_site(self, code="A01", capabilities=("光动力介入",)):
        site = self.c.register_site(code, "一中心", capabilities)
        return self.c.activate_site(site["id"])

    def batches(self):
        drug = self.c.register_material("药物", "D-01", "光敏剂")
        device = self.c.register_material("器械", "X-01", "球囊激光光纤")
        db = self.c.receive_batch(drug["id"], "D2026A", "2026-12-31", 20)
        xb = self.c.receive_batch(device["id"], "X2026A", "2026-12-31", 10)
        return db, xb

    def open_cohort(self, protocol, level=1, dose="0.5mg/kg", regimen=REG_R100):
        cohort = self.c.define_cohort(protocol["id"], f"C{level}", level,
                                      dose, regimen, 8)
        self.c.submit_cohort(cohort["id"])
        self.c.safety_review_cohort(cohort["id"], "放行", actor=C)
        return self.c.open_cohort(cohort["id"])

    def enrolled_subject(self, protocol, code="S01", site=None):
        site = site or self.active_site()
        s = self.c.register_subject(site["id"], code)
        self.c.sign_consent(s["id"], protocol["id"])
        self.c.record_eligibility(s["id"], {"I1局部肿瘤": True, "I2可签署": True},
                                  {"E1远处转移": False})
        return self.c.enroll_subject(s["id"])


class ProtocolGateTest(FlowTestBase):
    def test_activation_requires_safety_release(self):
        p = self.c.draft_protocol("V1.0", ["0.5mg/kg"], [REG_R100])
        expect_error(self, TrialErrorCode.SAFETY_GATE, self.c.activate_protocol, p["id"])
        self.c.submit_protocol(p["id"])
        expect_error(self, TrialErrorCode.FORBIDDEN,
                     self.c.safety_review_protocol, p["id"], "放行", actor=INV)
        self.c.safety_review_protocol(p["id"], "放行", actor=C)
        active = self.c.activate_protocol(p["id"])
        self.assertEqual(active["status"], "已激活")

    def test_rejected_protocol_returns_to_draft_and_keeps_gate(self):
        p = self.c.draft_protocol("V1.0", ["0.5mg/kg"], [REG_R100])
        self.c.submit_protocol(p["id"])
        reviewed = self.c.safety_review_protocol(p["id"], "驳回", "剂量爬坡依据不足", actor=C)
        self.assertEqual(reviewed["status"], "草稿")
        expect_error(self, TrialErrorCode.SAFETY_GATE, self.c.activate_protocol, p["id"])

    def test_new_version_activation_supersedes_old(self):
        v1 = self.activate_protocol("V1.0")
        v2 = self.c.draft_protocol("V2.0", ["0.5mg/kg"], [REG_R100])
        self.c.submit_protocol(v2["id"])
        self.c.safety_review_protocol(v2["id"], "放行", actor=C)
        self.c.activate_protocol(v2["id"])
        self.assertEqual(self.c.store.get("protocols", v1["id"])["status"], "已停用")
        self.assertEqual(self.c.active_protocol()["version"], "V2.0")

    def test_consent_requires_active_version(self):
        v1 = self.activate_protocol("V1.0")
        site = self.active_site()
        s = self.c.register_subject(site["id"], "S01")
        draft = self.c.draft_protocol("V2.0", ["0.5mg/kg"], [REG_R100])
        expect_error(self, TrialErrorCode.INVALID_STATE,
                     self.c.sign_consent, s["id"], draft["id"])
        self.c.sign_consent(s["id"], v1["id"])  # 激活版本可签


class SiteAndMaterialTest(FlowTestBase):
    def test_site_without_capability_cannot_activate_or_enroll(self):
        site = self.c.register_site("A09", "新中心", ("普通介入",))
        expect_error(self, TrialErrorCode.VALIDATION, self.c.activate_site, site["id"])
        self.c.store.update("sites", site["id"], capabilities=["光动力介入"])
        active = self.c.activate_site(site["id"])
        self.assertEqual(active["status"], "已激活")

    def test_suspended_site_blocks_enrollment(self):
        protocol = self.activate_protocol()
        site = self.active_site()
        self.c.suspend_site(site["id"], "资质复核")
        s = self.c.register_subject(site["id"], "S01")
        self.c.sign_consent(s["id"], protocol["id"])
        self.c.record_eligibility(s["id"], {"I1": True}, {})
        expect_error(self, TrialErrorCode.INVALID_STATE, self.c.enroll_subject, s["id"])

    def test_quarantined_and_expired_batches_unusable(self):
        protocol = self.activate_protocol()
        cohort = self.open_cohort(protocol)
        s = self.enrolled_subject(protocol)
        drug = self.c.register_material("药物", "D-09", "药")
        device = self.c.register_material("器械", "X-09", "械")
        bad = self.c.receive_batch(drug["id"], "BAD", "2026-12-31", 1)
        good = self.c.receive_batch(device["id"], "OK", "2026-12-31", 1)
        self.c.quarantine_batch(bad["id"], "无菌异常")
        expect_error(self, TrialErrorCode.INVALID_STATE,
                     self.c.assign_cohort, s["id"], cohort["id"], bad["id"], good["id"])
        self.c.release_batch(bad["id"])
        expired = self.c.receive_batch(drug["id"], "OLD", "2025-01-01", 1)
        expect_error(self, TrialErrorCode.INVALID_STATE,
                     self.c.assign_cohort, s["id"], cohort["id"], expired["id"], good["id"])


class EnrollmentTest(FlowTestBase):
    def test_consent_and_eligibility_required(self):
        protocol = self.activate_protocol()
        site = self.active_site()
        s = self.c.register_subject(site["id"], "S01")
        expect_error(self, TrialErrorCode.VALIDATION, self.c.enroll_subject, s["id"])
        self.c.sign_consent(s["id"], protocol["id"])
        expect_error(self, TrialErrorCode.VALIDATION, self.c.enroll_subject, s["id"])
        self.c.record_eligibility(s["id"], {"I1": True}, {"E1远处转移": True})
        self.assertEqual(self.c.store.get("subjects", s["id"])["status"], "筛选失败")
        expect_error(self, TrialErrorCode.INVALID_STATE, self.c.enroll_subject, s["id"])

    def test_consent_version_superseded_blocks_enrollment_until_resigned(self):
        v1 = self.activate_protocol("V1.0")
        site = self.active_site()
        s = self.c.register_subject(site["id"], "S01")
        self.c.sign_consent(s["id"], v1["id"])
        self.c.record_eligibility(s["id"], {"I1": True}, {})
        v2 = self.c.draft_protocol("V2.0", ["0.5mg/kg"], [REG_R100])
        self.c.submit_protocol(v2["id"])
        self.c.safety_review_protocol(v2["id"], "放行", actor=C)
        self.c.activate_protocol(v2["id"])
        expect_error(self, TrialErrorCode.VERSION_CONFLICT, self.c.enroll_subject, s["id"])
        # 旧同意被标记 superseded，按新版本重新签署后可入组
        self.c.sign_consent(s["id"], v2["id"])
        enrolled = self.c.enroll_subject(s["id"])
        self.assertEqual(enrolled["protocolVersion"], "V2.0")


class CohortSafetyGateTest(FlowTestBase):
    def test_open_requires_release(self):
        protocol = self.activate_protocol()
        cohort = self.c.define_cohort(protocol["id"], "C1", 1, "0.5mg/kg", REG_R100, 8)
        expect_error(self, TrialErrorCode.SAFETY_GATE, self.c.open_cohort, cohort["id"])

    def test_escalation_blocked_until_lower_cohort_open(self):
        protocol = self.activate_protocol()
        c2 = self.c.define_cohort(protocol["id"], "C2", 2, "1.0mg/kg", REG_R150, 8)
        expect_error(self, TrialErrorCode.SAFETY_GATE, self.c.submit_cohort, c2["id"])
        c1 = self.open_cohort(protocol, 1, "0.5mg/kg", REG_R100)
        # 仅开放仍不够：需要低水平队列产出两周安全数据
        expect_error(self, TrialErrorCode.SAFETY_GATE, self.c.submit_cohort, c2["id"])
        db, xb = self.batches()
        s = self.enrolled_subject(protocol)
        self.c.assign_cohort(s["id"], c1["id"], db["id"], xb["id"])
        self.c.administer_drug(s["id"], "2026-01-09T09:00")
        self.c.illuminate(s["id"], "2026-01-09T09:30", 100, 300)
        subject = self.c.finish_treatment(s["id"], "2026-01-09T10:00")
        day = subject["observationWindow"]["start"]
        for kind, seed in (("影像", "i"), ("病理", "p")):
            self.c.submit_evaluation_material(s["id"], kind, f"{kind}://1",
                                              checksum(seed), day)
            self.c.verify_checksum(s["id"], kind, checksum(seed))
        self.c.evaluability(s["id"])
        self.c.submit_cohort(c2["id"])  # 低水平已有可评估病例后可提交
        self.c.safety_review_cohort(c2["id"], "放行", actor=C)
        self.c.open_cohort(c2["id"])

    def test_cohort_dose_must_belong_to_protocol(self):
        protocol = self.activate_protocol(regimens=[REG_R100])
        expect_error(self, TrialErrorCode.VALIDATION, self.c.define_cohort,
                     protocol["id"], "C1", 1, "2.0mg/kg", REG_R100, 8)
        expect_error(self, TrialErrorCode.VALIDATION, self.c.define_cohort,
                     protocol["id"], "C1", 1, "0.5mg/kg", REG_R150, 8)

    def test_sae_freezes_escalation_and_resume_needs_release(self):
        protocol = self.activate_protocol()
        c1 = self.open_cohort(protocol, 1, "0.5mg/kg", REG_R100)
        db, xb = self.batches()
        s = self.enrolled_subject(protocol)
        self.c.assign_cohort(s["id"], c1["id"], db["id"], xb["id"])
        c2 = self.c.define_cohort(protocol["id"], "C2", 2, "1.0mg/kg", REG_R150, 8)
        event = self.c.report_event(s["id"], "严重AE", "2026-01-10T09:00",
                                    "术后重症胰腺炎", "张医生")
        self.assertEqual(self.c.store.get("cohorts", c1["id"])["status"], "暂停")
        expect_error(self, TrialErrorCode.SAFETY_GATE, self.c.submit_cohort, c2["id"])
        # 未处置 SAE 时即使尝试开放也被冻结闸门阻止
        expect_error(self, TrialErrorCode.SAFETY_GATE, self.c.open_cohort, c1["id"])
        self.c.notify_committee(event["id"], "2026-01-10T15:00")
        self.c.review_sae(event["id"], "放行", "与器械无关，恢复入组", actor=C)
        self.assertEqual(self.c.store.get("cohorts", c1["id"])["status"], "开放")
        # SAE 冻结解除，但低水平队列尚无两周安全数据，递进仍被闸门阻止
        expect_error(self, TrialErrorCode.SAFETY_GATE, self.c.submit_cohort, c2["id"])


class AssignmentTest(FlowTestBase):
    def test_blinded_role_cannot_assign_and_double_assignment_blocked(self):
        protocol = self.activate_protocol()
        cohort = self.open_cohort(protocol)
        db, xb = self.batches()
        s = self.enrolled_subject(protocol)
        expect_error(self, TrialErrorCode.FORBIDDEN, self.c.assign_cohort,
                     s["id"], cohort["id"], db["id"], xb["id"], actor=BLIND)
        self.c.assign_cohort(s["id"], cohort["id"], db["id"], xb["id"])
        expect_error(self, TrialErrorCode.CONFLICT, self.c.assign_cohort,
                     s["id"], cohort["id"], db["id"], xb["id"])

    def test_closed_cohort_rejects_assignment(self):
        protocol = self.activate_protocol()
        cohort = self.open_cohort(protocol)
        self.c.close_cohort(cohort["id"], "满组")
        db, xb = self.batches()
        s = self.enrolled_subject(protocol, code="S02")
        expect_error(self, TrialErrorCode.SAFETY_GATE, self.c.assign_cohort,
                     s["id"], cohort["id"], db["id"], xb["id"])

    def test_assignment_snapshot_freezes_dose_and_batches(self):
        protocol = self.activate_protocol()
        cohort = self.open_cohort(protocol)
        db, xb = self.batches()
        s = self.enrolled_subject(protocol)
        assignment = self.c.assign_cohort(s["id"], cohort["id"], db["id"], xb["id"])
        self.assertEqual(assignment["drugDose"], "0.5mg/kg")
        self.assertEqual(assignment["drugBatchNo"], "D2026A")
        self.c.quarantine_batch(db["id"], "召回")  # 隔离不改变已冻结快照
        snapshotted = self.c.store.find("assignments", subjectId=s["id"])
        self.assertEqual(snapshotted["drugBatchNo"], "D2026A")


class TreatmentExecutionTest(FlowTestBase):
    def _ready(self, code="S01"):
        protocol = self.activate_protocol()
        cohort = self.open_cohort(protocol)
        db, xb = self.batches()
        device2 = self.c.register_material("器械", "X-02", "备用球囊")
        xb2 = self.c.receive_batch(device2["id"], "X2026B", "2026-12-31", 5)
        s = self.enrolled_subject(protocol, code)
        self.c.assign_cohort(s["id"], cohort["id"], db["id"], xb["id"])
        return protocol, cohort, db, xb, xb2, s

    def test_wrong_drug_batch_blocked_without_deviation(self):
        _, _, db, xb, _, s = self._ready()
        other = self.c.receive_batch(
            self.c.register_material("药物", "D-02", "另一批号药")["id"],
            "D2026B", "2026-12-31", 5)
        expect_error(self, TrialErrorCode.CONFLICT, self.c.administer_drug,
                     s["id"], "2026-01-09T09:00", other["id"])
        # 无任何记录落入时间线（原子拒绝）
        self.assertEqual(self.c.store.list("timeline", subjectId=s["id"], node="给药"), [])
        # 正常批次可执行
        self.c.administer_drug(s["id"], "2026-01-09T09:00")

    def test_wrong_regimen_blocked_but_emergency_deviation_allows(self):
        _, _, _, xb, _, s = self._ready()
        self.c.administer_drug(s["id"], "2026-01-09T09:00")
        err = expect_error(self, TrialErrorCode.CONFLICT, self.c.illuminate,
                           s["id"], "2026-01-09T09:30", 150, 420)
        self.assertEqual(err.details["planned"], REG_R100)
        dv = self.c.emergency_deviation(s["id"], "激光输出异常，延长照射补偿",
                                        "王医生", at="2026-01-09T09:35")
        node = self.c.illuminate(s["id"], "2026-01-09T09:40", 100, 320,
                                 deviation_id=dv["id"])
        self.assertEqual(node["deviationId"], dv["id"])

    def test_device_change_rules(self):
        _, _, _, xb, xb2, s = self._ready()
        self.c.administer_drug(s["id"], "2026-01-09T09:00")
        # 照射前：登记原因即可更换
        change = self.c.change_device(s["id"], xb2["id"], "球囊密封异常",
                                      "2026-01-09T09:10")
        self.assertIsNone(change["deviationId"])
        self.c.illuminate(s["id"], "2026-01-09T09:20", 100, 300)
        # 照射后：无偏离单禁止更换
        expect_error(self, TrialErrorCode.INVALID_STATE, self.c.change_device,
                     s["id"], xb["id"], "光纤断裂", "2026-01-09T09:40")
        dv = self.c.emergency_deviation(s["id"], "光纤断裂更换", "王医生",
                                        at="2026-01-09T09:45")
        self.c.change_device(s["id"], xb["id"], "光纤断裂",
                             "2026-01-09T09:50", deviation_id=dv["id"])
        # 错误照光参数依然被阻止（器械更换不改方案）
        expect_error(self, TrialErrorCode.CONFLICT, self.c.illuminate,
                     s["id"], "2026-01-09T10:00", 150, 420)

    def test_timeline_must_be_strictly_increasing(self):
        _, _, _, _, _, s = self._ready()
        self.c.administer_drug(s["id"], "2026-01-09T09:00")
        expect_error(self, TrialErrorCode.DUPLICATE, self.c.administer_drug,
                     s["id"], "2026-01-09T09:00")  # 节点重复
        expect_error(self, TrialErrorCode.VALIDATION, self.c.illuminate,
                     s["id"], "2026-01-09T08:59", 100, 300)  # 早于给药
        self.c.illuminate(s["id"], "2026-01-09T09:30", 100, 300)

    def test_postponement_keeps_window_on_actual_end(self):
        _, _, _, _, _, s = self._ready()
        self.c.postpone_procedure(s["id"], "介入室排程冲突",
                                  "2026-01-09T08:00", "2026-01-15T09:00")
        self.c.administer_drug(s["id"], "2026-01-15T09:00")
        self.c.illuminate(s["id"], "2026-01-15T09:30", 100, 300)
        subject = self.c.finish_treatment(s["id"], "2026-01-15T10:00")
        # 结束日 1/15，第14天为 1/29，窗口 1/27~1/31
        self.assertEqual(subject["observationWindow"],
                         {"start": "2026-01-27", "end": "2026-01-31"})


class DeviationTest(FlowTestBase):
    def test_supplement_overdue_flagged_and_review_gate(self):
        protocol = self.activate_protocol()
        cohort = self.open_cohort(protocol)
        db, xb = self.batches()
        s = self.enrolled_subject(protocol)
        self.c.assign_cohort(s["id"], cohort["id"], db["id"], xb["id"])
        dv = self.c.emergency_deviation(s["id"], "先行处置摘要", "王医生",
                                        at="2026-01-09T09:00")
        self.assertEqual(dv["status"], "待复核")
        expect_error(self, TrialErrorCode.OVERDUE, self.c.review_deviation,
                     dv["id"], "确认", actor=C)
        expect_error(self, TrialErrorCode.FORBIDDEN, self.c.review_deviation,
                     dv["id"], "确认", actor=INV)
        late = self.c.supplement_deviation(dv["id"], "26 小时后补录完整原因",
                                           at="2026-01-10T11:00")
        self.assertTrue(late["overdue"])
        reviewed = self.c.review_deviation(dv["id"], "确认", actor=C)
        self.assertEqual(reviewed["status"], "已确认")

    def test_rejected_deviation_cannot_support_operation(self):
        protocol = self.activate_protocol()
        cohort = self.open_cohort(protocol)
        db, xb = self.batches()
        s = self.enrolled_subject(protocol, code="S02")
        self.c.assign_cohort(s["id"], cohort["id"], db["id"], xb["id"])
        self.c.administer_drug(s["id"], "2026-01-09T09:00")
        dv = self.c.emergency_deviation(s["id"], "摘要", "王医生",
                                        at="2026-01-09T09:10")
        self.c.supplement_deviation(dv["id"], "原因", at="2026-01-09T12:00")
        self.c.review_deviation(dv["id"], "驳回", "理由不成立", actor=C)
        expect_error(self, TrialErrorCode.INVALID_STATE, self.c.illuminate,
                     s["id"], "2026-01-09T09:30", 150, 420,
                     deviation_id=dv["id"])


class EvaluationWindowTest(FlowTestBase):
    def _in_observation(self, code="S01", end="2026-01-15T10:00"):
        protocol = self.activate_protocol()
        cohort = self.open_cohort(protocol)
        db, xb = self.batches()
        s = self.enrolled_subject(protocol, code)
        self.c.assign_cohort(s["id"], cohort["id"], db["id"], xb["id"])
        end_dt = datetime.datetime.fromisoformat(end)
        self.c.administer_drug(s["id"], end_dt.replace(hour=9, minute=0))
        self.c.illuminate(s["id"], end_dt.replace(hour=9, minute=30), 100, 300)
        subject = self.c.finish_treatment(s["id"], end)
        return s, subject

    def test_out_of_window_and_bad_checksum_rejected(self):
        s, subject = self._in_observation()
        start = subject["observationWindow"]["start"]
        outside = (datetime.date.fromisoformat(start) - datetime.timedelta(days=3)).isoformat()
        expect_error(self, TrialErrorCode.WINDOW_VIOLATION,
                     self.c.submit_evaluation_material,
                     s["id"], "影像", "img://deid/001", checksum("img"), outside)
        expect_error(self, TrialErrorCode.VALIDATION,
                     self.c.submit_evaluation_material,
                     s["id"], "影像", "img://deid/001", "abc", start)
        expect_error(self, TrialErrorCode.VALIDATION,
                     self.c.submit_evaluation_material,
                     s["id"], "影像", "", checksum("img"), start)
        # 边界日（窗口两端含端点）应接受
        self.c.submit_evaluation_material(s["id"], "影像", "img://deid/001",
                                          checksum("img"), start)

    def test_window_endpoints_inclusive_and_checksum_verification(self):
        s, subject = self._in_observation()
        end = subject["observationWindow"]["end"]
        self.c.submit_evaluation_material(s["id"], "影像", "img://deid/001",
                                          checksum("img"), end)
        self.c.submit_evaluation_material(s["id"], "病理", "path://deid/001",
                                          checksum("path"), end)
        expect_error(self, TrialErrorCode.CHECKSUM_MISMATCH,
                     self.c.verify_checksum, s["id"], "影像", checksum("tampered"))
        self.c.verify_checksum(s["id"], "影像", checksum("img"))
        self.c.verify_checksum(s["id"], "病理", checksum("path"))
        subject, reasons = self.c.evaluability(s["id"])
        self.assertEqual(subject["status"], "可评估")
        self.assertEqual(reasons, [])

    def test_missing_material_keeps_not_evaluable(self):
        s, subject = self._in_observation()
        day = subject["observationWindow"]["start"]
        self.c.submit_evaluation_material(s["id"], "影像", "img://1",
                                          checksum("img"), day)
        self.c.verify_checksum(s["id"], "影像", checksum("img"))
        subject, reasons = self.c.evaluability(s["id"])
        self.assertEqual(subject["status"], "观察中")
        self.assertIn("缺少病理材料", reasons)


class OutcomeProvenanceTest(FlowTestBase):
    def test_outcome_requires_evaluable_and_keeps_full_chain(self):
        protocol = self.activate_protocol()
        cohort = self.open_cohort(protocol)
        db, xb = self.batches()
        s = self.enrolled_subject(protocol)
        assignment = self.c.assign_cohort(s["id"], cohort["id"], db["id"], xb["id"])
        self.c.administer_drug(s["id"], "2026-01-15T09:00")
        self.c.illuminate(s["id"], "2026-01-15T09:30", 100, 300)
        self.c.finish_treatment(s["id"], "2026-01-15T10:00")
        expect_error(self, TrialErrorCode.INVALID_STATE, self.c.record_outcome,
                     s["id"], "可切除", "手术切除", "外科主任", "2026-01-29T10:00")
        for kind, seed in (("影像", "img"), ("病理", "path")):
            self.c.submit_evaluation_material(s["id"], kind, f"{kind}://deid/1",
                                              checksum(seed), "2026-01-29")
            self.c.verify_checksum(s["id"], kind, checksum(seed))
        self.c.evaluability(s["id"])
        outcome = self.c.record_outcome(s["id"], "可切除", "残余肿瘤 R0 切除",
                                        "外科主任", "2026-01-29T15:00")
        chain = outcome["provenance"]
        self.assertEqual(chain["protocolVersion"], "V1.0")
        self.assertEqual(chain["drugDose"], "0.5mg/kg")
        self.assertEqual(chain["drugBatchNo"], "D2026A")
        self.assertEqual(chain["deviceBatchNo"], "X2026A")
        nodes = [n for n, _ in chain["timeline"]]
        self.assertEqual(nodes, ["同意", "入组", "给药", "照射",
                                 "治疗结束", "两周评估"])
        expect_error(self, TrialErrorCode.DUPLICATE, self.c.record_outcome,
                     s["id"], "不可切除", "重复登记", "主任", "2026-01-30T10:00")


class WithdrawalTest(FlowTestBase):
    def test_withdrawal_stops_new_use_but_keeps_safety_records(self):
        protocol = self.activate_protocol()
        cohort = self.open_cohort(protocol)
        db, xb = self.batches()
        s = self.enrolled_subject(protocol)
        self.c.assign_cohort(s["id"], cohort["id"], db["id"], xb["id"])
        self.c.administer_drug(s["id"], "2026-01-15T09:00")
        self.c.withdraw_subject(s["id"], "个人原因", "2026-01-16T10:00")
        # 新增研究用途全部停止
        expect_error(self, TrialErrorCode.INVALID_STATE, self.c.illuminate,
                     s["id"], "2026-01-16T11:00", 100, 300)
        expect_error(self, TrialErrorCode.INVALID_STATE,
                     self.c.emergency_deviation, s["id"], "x", "王医生")
        # 安全随访与法规记录保留：撤回后仍可报告 AE
        event = self.c.report_event(s["id"], "一般AE", "2026-01-18T09:00",
                                    "随访皮疹", "随访护士")
        self.assertEqual(event["subjectId"], s["id"])
        provenance = self.c.provenance(s["id"])
        self.assertEqual(provenance["subject"]["status"], "已撤回")
        self.assertIsNotNone(provenance["assignment"])
        self.assertEqual(len(provenance["timeline"]), 3)  # 同意/入组/给药保留
        self.assertEqual(len(provenance["events"]), 1)
        subject, reasons = self.c.evaluability(s["id"])
        self.assertIn("受试者已撤回", reasons)

    def test_observation_window_material_blocked_after_withdrawal(self):
        protocol = self.activate_protocol()
        cohort = self.open_cohort(protocol)
        db, xb = self.batches()
        s = self.enrolled_subject(protocol, code="S03")
        self.c.assign_cohort(s["id"], cohort["id"], db["id"], xb["id"])
        self.c.administer_drug(s["id"], "2026-01-15T09:00")
        self.c.illuminate(s["id"], "2026-01-15T09:30", 100, 300)
        self.c.finish_treatment(s["id"], "2026-01-15T10:00")
        self.c.withdraw_subject(s["id"], "撤回同意", "2026-01-20T10:00")
        expect_error(self, TrialErrorCode.INVALID_STATE,
                     self.c.submit_evaluation_material,
                     s["id"], "影像", "img://1", checksum("img"), "2026-01-29")


class BlindedViewTest(FlowTestBase):
    def test_blinded_role_cannot_see_cohort_information(self):
        protocol = self.activate_protocol()
        cohort = self.open_cohort(protocol)
        db, xb = self.batches()
        s = self.enrolled_subject(protocol)
        self.c.assign_cohort(s["id"], cohort["id"], db["id"], xb["id"])
        self.c.administer_drug(s["id"], "2026-01-15T09:00")
        self.c.illuminate(s["id"], "2026-01-15T09:30", 100, 300)
        self.c.finish_treatment(s["id"], "2026-01-15T10:00")
        self.c.submit_evaluation_material(s["id"], "影像", "img://1",
                                          checksum("img"), "2026-01-29")
        blinded = self.c.subject_view(s["id"], BLIND)
        self.assertTrue(blinded["blinded"])
        self.assertIsNone(blinded["assignment"])
        self.assertIsNone(blinded["protocol"])
        self.assertIsNone(blinded["events"])
        self.assertEqual(len(blinded["evaluations"]), 1)  # 去标识化材料可见
        visible_subject = blinded["subject"]
        self.assertNotIn("protocolVersion", visible_subject)
        self.assertEqual(blinded["timeline"][0]["node"], "治疗结束")
        for node in blinded["timeline"]:
            self.assertNotIn("deviceBatchId", node)
        # 非盲角色看到完整链
        full = self.c.subject_view(s["id"], INV)
        self.assertFalse(full["blinded"])
        self.assertEqual(full["assignment"]["drugDose"], "0.5mg/kg")


class ObservationWindowPureTest(unittest.TestCase):
    def test_window_is_day14_plus_minus_2(self):
        start, end = observation_window("2026-01-15")
        self.assertEqual(start, datetime.date(2026, 1, 27))
        self.assertEqual(end, datetime.date(2026, 1, 31))


if __name__ == "__main__":
    unittest.main()
