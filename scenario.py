"""六名受试者并发安排的端到端场景回放。

运行：python3 scenario.py（全部断言通过时退出码 0）。

场景覆盖：
  S01 标准路径：C1 队列 -> 治疗 -> 观察窗内双材料 -> 可评估 -> 可切除
  S02 照射前器械更换（X2601 -> X2602），不影响方案与窗口
  S03 术期延后（02-06 -> 02-12），观察窗按实际治疗结束日重算
  S04 治疗后 SAE：队列自动暂停、C2 递进冻结、24h 内报告、委员会复核后恢复
  S05 错误剂量队列（C2 未放行）与错误照光参数被阻止；激光异常紧急偏离，
      24h 内补录原因并经委员会确认后完成治疗
  S06 治疗结束后撤回：新增研究用途全部停止，安全随访记录继续保留

另含：六线程并发入组分配、盲态视图遮蔽、每个结局的完整溯源断言。
"""

import datetime
import hashlib
import threading

from trial import TrialCoordinator, TrialError
from trial.store import EventStore, fixed_clock

C = "安全委员会"
INV = "研究者"
BLIND = "盲态评价者"

R100 = {"energy": 100, "durationMinutes": 300}
R150 = {"energy": 150, "durationMinutes": 420}

_passed = []
_failed = []


def check(name, condition, detail=""):
    (_passed if condition else _failed).append(name)
    mark = "PASS" if condition else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" —— {detail}"
    print(line)
    if not condition:
        raise AssertionError(name + (f": {detail}" if detail else ""))


def check_raises(name, code, func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except TrialError as error:
        check(name, error.code == code, f"{error.code}: {error.message}")
        return error
    check(name, False, "应当被拒绝但操作成功")
    return None


def sha(seed):
    return hashlib.sha256(seed.encode()).hexdigest()


def main():
    clock = fixed_clock("2026-02-02T08:00:00")
    t = TrialCoordinator(EventStore(clock))

    print("== 1. 方案版本与安全放行 ==")
    p = t.draft_protocol("V1.0", ["0.5mg/kg", "1.0mg/kg"], [R100, R150],
                         "光动力 I 期，两周后评估可切除性")
    t.submit_protocol(p["id"])
    check_raises("研究者不能放行方案", "FORBIDDEN",
                 t.safety_review_protocol, p["id"], "放行", actor=INV)
    t.safety_review_protocol(p["id"], "放行", "剂量与照光参数可接受", actor=C)
    t.activate_protocol(p["id"])
    check("V1.0 已激活", t.active_protocol()["version"] == "V1.0")

    print("== 2. 中心资质、物料批次 ==")
    site = t.register_site("A01", "一中心", ("光动力介入", "胰腺外科"))
    t.activate_site(site["id"])
    drug = t.register_material("药物", "D-PS", "光敏剂 PS-118")
    device = t.register_material("器械", "X-BAL", "带激光光纤球囊")
    db = t.receive_batch(drug["id"], "D2601", "2026-12-31", 24)
    xb = t.receive_batch(device["id"], "X2601", "2026-12-31", 12)
    xb2 = t.receive_batch(device["id"], "X2602", "2026-12-31", 6)

    print("== 3. 剂量队列：C1 放行开放，C2 待递进 ==")
    c1 = t.define_cohort(p["id"], "低剂量", 1, "0.5mg/kg", R100, 8)
    t.submit_cohort(c1["id"])
    t.safety_review_cohort(c1["id"], "放行", actor=C)
    t.open_cohort(c1["id"])
    c2 = t.define_cohort(p["id"], "高剂量", 2, "1.0mg/kg", R150, 8)
    check_raises("低水平未出数据前 C2 不能递进", "SAFETY_GATE",
                 t.submit_cohort, c2["id"])

    print("== 4. 六名受试者建档/同意/入排，六线程并发入组分配 ==")
    subjects = {}
    for code in ("S01", "S02", "S03", "S04", "S05", "S06"):
        s = t.register_subject(site["id"], code)
        t.sign_consent(s["id"], p["id"], doc_ref=f"icf://deid/{code}",
                       doc_checksum=sha(f"icf-{code}"))
        t.record_eligibility(s["id"],
                             {"I1不可直接手术": True, "I2局部肿瘤": True,
                              "I3可签署同意": True},
                             {"E1远处转移": False, "E2凝血障碍": False})
        subjects[code] = s

    barrier = threading.Barrier(6)
    errors = []

    def worker(code):
        try:
            barrier.wait()
            t.enroll_subject(subjects[code]["id"])
            t.assign_cohort(subjects[code]["id"], c1["id"], db["id"], xb["id"])
        except Exception as exc:  # noqa: BLE001 - 汇总并发线程错误
            errors.append((code, exc))

    threads = [threading.Thread(target=worker, args=(code,))
               for code in subjects]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    check("六个并发入组分配全部成功", not errors, str(errors))
    check("C1 入组计数为 6", t.store.get("cohorts", c1["id"])["enrolled"] == 6)
    check("六人均处治疗中",
          all(t.store.get("subjects", s["id"])["status"] == "治疗中"
              for s in subjects.values()))

    print("== 5. S05 错误方案尝试：已分配者不能改队列（防剂量混淆） ==")
    s05 = subjects["S05"]
    check_raises("已分配 C1 者不能改投 C2（剂量混淆防护）", "CONFLICT",
                 t.assign_cohort, s05["id"], c2["id"], db["id"], xb["id"])
    check_raises("C2 未安全放行不能开放入组", "SAFETY_GATE",
                 t.open_cohort, c2["id"])
    # 其分配快照仍是 C1/0.5mg/kg
    a05 = t.store.find("assignments", subjectId=s05["id"])
    check("S05 分配快照锁定 C1 低剂量",
          a05["level"] == 1 and a05["drugDose"] == "0.5mg/kg")

    print("== 6. S01 标准治疗与两周评估 ==")
    t.administer_drug(subjects["S01"]["id"], "2026-02-05T09:00")
    t.illuminate(subjects["S01"]["id"], "2026-02-05T09:30", 100, 300)
    s01 = t.finish_treatment(subjects["S01"]["id"], "2026-02-05T10:00")
    check("S01 观察窗 02-17~02-21（第14天±2）",
          s01["observationWindow"] == {"start": "2026-02-17", "end": "2026-02-21"})
    check_raises("S01 越窗影像（02-16）拒收", "WINDOW_VIOLATION",
                 t.submit_evaluation_material, subjects["S01"]["id"], "影像",
                 "img://deid/S01/1", sha("s01-img"), "2026-02-16")
    t.submit_evaluation_material(subjects["S01"]["id"], "影像",
                                 "img://deid/S01/1", sha("s01-img"), "2026-02-19")
    t.submit_evaluation_material(subjects["S01"]["id"], "病理",
                                 "path://deid/S01/1", sha("s01-path"), "2026-02-19")
    check_raises("篡改校验值核验失败", "CHECKSUM_MISMATCH",
                 t.verify_checksum, subjects["S01"]["id"], "影像",
                 sha("tampered"))
    t.verify_checksum(subjects["S01"]["id"], "影像", sha("s01-img"))
    t.verify_checksum(subjects["S01"]["id"], "病理", sha("s01-path"))
    s01, reasons = t.evaluability(subjects["S01"]["id"])
    check("S01 可评估", s01["status"] == "可评估" and reasons == [])
    o01 = t.record_outcome(subjects["S01"]["id"], "可切除",
                           "两周后影像缩小，行 R0 切除", "赵外科",
                           "2026-02-19T15:00")

    print("== 7. S02 照射前器械更换 ==")
    s02 = subjects["S02"]
    change = t.change_device(s02["id"], xb2["id"], "术前测试球囊密封异常",
                             "2026-02-06T08:40")
    check("更换记录了原批次与新批次",
          change["fromBatchId"] == xb["id"] and change["toBatchId"] == xb2["id"])
    t.administer_drug(s02["id"], "2026-02-06T09:00")
    t.illuminate(s02["id"], "2026-02-06T09:30", 100, 300)
    t.finish_treatment(s02["id"], "2026-02-06T10:00")
    for kind, seed in (("影像", "s02-img"), ("病理", "s02-path")):
        t.submit_evaluation_material(s02["id"], kind, f"{kind}://deid/S02/1",
                                     sha(seed), "2026-02-20")
        t.verify_checksum(s02["id"], kind, sha(seed))
    t.evaluability(s02["id"])
    o02 = t.record_outcome(s02["id"], "可切除", "边缘清晰，手术切除",
                           "赵外科", "2026-02-20T15:00")
    check("S02 溯源器械为更换后 X2602，且记录 1 次更换",
          o02["provenance"]["deviceBatchNo"] == "X2602"
          and o02["provenance"]["deviceChanges"] == 1)

    print("== 8. S03 术期延后，窗口随实际结束日移动 ==")
    s03 = subjects["S03"]
    t.postpone_procedure(s03["id"], "介入室急诊排程冲突",
                         "2026-02-06T07:30", "2026-02-12T09:00")
    t.administer_drug(s03["id"], "2026-02-12T09:00")
    t.illuminate(s03["id"], "2026-02-12T09:30", 100, 300)
    s03 = t.finish_treatment(s03["id"], "2026-02-12T10:00")
    check("S03 窗口按实际结束日 02-12 计算为 02-24~02-28",
          s03["observationWindow"] == {"start": "2026-02-24", "end": "2026-02-28"})
    check_raises("按旧计划日 02-20 提交材料属越窗", "WINDOW_VIOLATION",
                 t.submit_evaluation_material, s03["id"], "影像",
                 "img://deid/S03/1", sha("s03-img"), "2026-02-20")
    for kind, seed in (("影像", "s03-img"), ("病理", "s03-path")):
        t.submit_evaluation_material(s03["id"], kind, f"{kind}://deid/S03/1",
                                     sha(seed), "2026-02-25")
        t.verify_checksum(s03["id"], kind, sha(seed))
    t.evaluability(s03["id"])
    o03 = t.record_outcome(s03["id"], "不可切除",
                           "仍包绕肠系膜血管，转化疗", "赵外科",
                           "2026-02-25T16:00")

    print("== 9. S04 SAE：队列暂停、递进冻结、24h 报告、复核恢复 ==")
    s04 = subjects["S04"]
    t.administer_drug(s04["id"], "2026-02-07T09:00")
    t.illuminate(s04["id"], "2026-02-07T09:30", 100, 300)
    t.finish_treatment(s04["id"], "2026-02-07T10:00")
    sae = t.report_event(s04["id"], "严重AE", "2026-02-07T14:00",
                         "术后重症胰腺炎，ICU 监护", "病房医生",
                         related={"phase": "治疗后"})
    check("SAE 发生后 C1 自动暂停",
          t.store.get("cohorts", c1["id"])["status"] == "暂停")
    check_raises("SAE 未处置期间 C2 递进冻结", "SAFETY_GATE",
                 t.submit_cohort, c2["id"])
    check_raises("SAE 未处置期间 C1 不能重新开放", "SAFETY_GATE",
                 t.open_cohort, c1["id"])
    t.notify_committee(sae["id"], "2026-02-07T20:00")  # 6 小时内报告
    event_after = t.store.get("events", sae["id"])
    check("SAE 委员会报告未超 24h", event_after["reportOverdue"] is False)
    t.review_sae(sae["id"], "放行", "判定与剂量/器械无因果，恢复入组",
                 actor=C)
    check("委员会复核后 C1 恢复开放",
          t.store.get("cohorts", c1["id"])["status"] == "开放")
    # SAE 不影响本人观察窗评估
    for kind, seed in (("影像", "s04-img"), ("病理", "s04-path")):
        t.submit_evaluation_material(s04["id"], kind, f"{kind}://deid/S04/1",
                                     sha(seed), "2026-02-21")
        t.verify_checksum(s04["id"], kind, sha(seed))
    s04, _ = t.evaluability(s04["id"])
    check("S04 观察窗与可评估状态不受队列暂停影响", s04["status"] == "可评估")
    o04 = t.record_outcome(s04["id"], "待定",
                           "SAE 恢复中，切除决策推迟至下次评估", "赵外科",
                           "2026-02-21T15:00")
    # SAE 处置后 C2 才允许进入放行流程（本场景不开放，保留递进凭证）
    t.submit_cohort(c2["id"])
    check("C2 已可进入待安全放行",
          t.store.get("cohorts", c2["id"])["status"] == "待安全放行")

    print("== 10. S05 紧急偏离：错误参数先阻止，先行处置后补录复核 ==")
    t.administer_drug(s05["id"], "2026-02-08T09:00")
    check_raises("错误照光参数（150/420）无偏离单被阻止", "CONFLICT",
                 t.illuminate, s05["id"], "2026-02-08T09:30", 150, 420)
    check("被阻止的错误照射未污染时间线",
          not any(n["node"] == "照射"
                  for n in t.store.list("timeline", subjectId=s05["id"])))
    dv = t.emergency_deviation(s05["id"], "激光功率波动，先行调整参数完成照射",
                               "王介入", at="2026-02-08T09:35",
                               category="照光参数偏离")
    node = t.illuminate(s05["id"], "2026-02-08T09:40", 100, 320,
                        deviation_id=dv["id"])
    check("带紧急偏离的实际参数 100J/320s 被记录并关联偏离单",
          node["energy"] == 100 and node["durationMinutes"] == 320
          and node["deviationId"] == dv["id"])
    t.finish_treatment(s05["id"], "2026-02-08T10:10")
    check_raises("未补录原因不能复核", "OVERDUE",
                 t.review_deviation, dv["id"], "确认", actor=C)
    t.supplement_deviation(dv["id"],
                           "功率波动 ±8%，按 SOP 将时长延长 20s 补偿能量，"
                           "受试者无异常", at="2026-02-08T20:00")
    dv_after = t.store.get("deviations", dv["id"])
    check("偏离 10.5 小时内补录，未超 24h", dv_after["overdue"] is False)
    t.review_deviation(dv["id"], "确认", "处置符合 SOP", actor=C)
    for kind, seed in (("影像", "s05-img"), ("病理", "s05-path")):
        t.submit_evaluation_material(s05["id"], kind, f"{kind}://deid/S05/1",
                                     sha(seed), "2026-02-22")
        t.verify_checksum(s05["id"], kind, sha(seed))
    t.evaluability(s05["id"])
    o05 = t.record_outcome(s05["id"], "可切除", "影像部分缓解，安排切除",
                           "赵外科", "2026-02-22T15:00")
    check("S05 结局可追到偏离单",
          o05["provenance"]["deviations"] == [dv["id"]])

    print("== 11. S06 治疗后撤回：停新增、保安全 ==")
    s06 = subjects["S06"]
    t.administer_drug(s06["id"], "2026-02-09T09:00")
    t.illuminate(s06["id"], "2026-02-09T09:30", 100, 300)
    t.finish_treatment(s06["id"], "2026-02-09T10:00")
    t.withdraw_subject(s06["id"], "不愿继续研究随访", "2026-02-11T09:00")
    check_raises("撤回后新增治疗被阻止", "INVALID_STATE",
                 t.illuminate, s06["id"], "2026-02-11T10:00", 100, 300)
    check_raises("撤回后评估材料拒收", "INVALID_STATE",
                 t.submit_evaluation_material, s06["id"], "影像",
                 "img://deid/S06/1", sha("s06-img"), "2026-02-23")
    check_raises("撤回后不能登记结局", "INVALID_STATE",
                 t.record_outcome, s06["id"], "可切除", "x", "赵外科",
                 "2026-02-23T10:00")
    followup_ae = t.report_event(s06["id"], "一般AE", "2026-02-13T10:00",
                                 "电话随访报告轻度皮疹", "随访护士")
    check("撤回后安全随访事件仍可记录", followup_ae["id"].startswith("AE-"))
    p06 = t.provenance(s06["id"])
    check("撤回者既有时间线/分配/同意全部保留",
          [n["node"] for n in p06["timeline"]]
          == ["同意", "入组", "给药", "照射", "治疗结束"]
          and p06["assignment"] is not None)

    print("== 12. 盲态视图遮蔽 ==")
    blind = t.subject_view(subjects["S01"]["id"], BLIND)
    dumped = str(blind)
    check("盲态视图无队列/剂量/批次/方案字样",
          "0.5mg/kg" not in dumped and "D2601" not in dumped
          and "X2601" not in dumped and "V1.0" not in dumped
          and blind["assignment"] is None and blind["outcome"] is None)
    check("盲态仍可见去标识化材料引用与校验值",
          [m["deidentifiedRef"] for m in blind["evaluations"]]
          == ["img://deid/S01/1", "path://deid/S01/1"])

    print("== 13. 全部结局溯源到获批方案/实际剂量/器械/医学决定 ==")
    for code, outcome in (("S01", o01), ("S02", o02), ("S03", o03),
                          ("S04", o04), ("S05", o05)):
        chain = outcome["provenance"]
        check(f"{code} 溯源链完整（V1.0/剂量/药批/械批/时间线/决定）",
              chain["protocolVersion"] == "V1.0"
              and chain["drugDose"] in ("0.5mg/kg", "1.0mg/kg")
              and chain["drugBatchNo"] == "D2601"
              and chain["deviceBatchNo"].startswith("X260")
              and len(chain["timeline"]) >= 5
              and outcome["decidedBy"] == "赵外科",
              detail=str(chain))

    audit = t.audit_trail()
    check("审计日志覆盖全部关键变更", len(audit) >= 60, f"共 {len(audit)} 条")

    print()
    print(f"场景完成：通过 {len(_passed)} 项，失败 {len(_failed)} 项。")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
