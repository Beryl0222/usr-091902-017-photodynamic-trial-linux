"""受控流程核心引擎。

一个 ``TrialCoordinator`` 实例绑定一个 EventStore，对外暴露领域命令。
所有命令在 store 锁内执行并做规则校验：任何规则冲突都抛
``TrialError``，冲突时不产生业务记录。

关键闸门：
* 方案版本：草稿 -> 提交 -> 安全委员会放行 -> 激活；新版本激活自动停用旧版本。
* 剂量队列：开放入组必须有该队列（绑定方案版本）的安全放行；SAE 自动暂停，
  未处置 SAE 阻止所有队列递进，恢复需重新放行。
* 治疗执行：实际药物批次/剂量/照光方案必须与分配快照一致，任何偏离必须
  关联紧急偏离单；时间线节点必须严格递增。
* 两周评估：影像与病理只存去标识化引用+校验值，采集日期必须落在观察窗内。
* 撤回：停止新增研究用途，既有安全记录保留并可随访。
"""

import datetime
import hashlib
import re

from trial import catalog
from trial.errors import TrialError, TrialErrorCode
from trial.store import EventStore

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_CAPABILITY = "光动力介入"


def _dt(value):
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    return datetime.datetime.fromisoformat(value)


def _date(value):
    return _dt(value).date()


def _role(actor):
    if isinstance(actor, dict):
        return actor.get("role")
    return actor


def _hours(a, b):
    return (_dt(a) - _dt(b)).total_seconds() / 3600.0


class TrialCoordinator:
    def __init__(self, store=None, clock=None):
        self.store = store or EventStore(clock=clock)

    # ------------------------------------------------------------------ #
    # 方案版本
    # ------------------------------------------------------------------ #
    def draft_protocol(self, version, drug_levels, light_regimens, description="", actor="研究者"):
        if self.store.find("protocols", version=version):
            raise TrialError(TrialErrorCode.DUPLICATE, f"方案版本已存在：{version}")
        if not drug_levels or not light_regimens:
            raise TrialError(TrialErrorCode.VALIDATION, "方案必须声明药物剂量水平与照光方案")
        return self.store.create("protocols", {
            "version": version,
            "drugLevels": list(drug_levels),
            "lightRegimens": list(light_regimens),
            "description": description,
            "status": catalog.PROTOCOL_STATUS[0],
            "createdBy": _role(actor),
        })

    def submit_protocol(self, protocol_id, actor="研究者"):
        protocol = self.store.require("protocols", protocol_id)
        if protocol["status"] != "草稿":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"方案当前为{protocol['status']}，不能提交", {"status": protocol["status"]})
        self.store.create("submissions", {"protocolId": protocol_id, "by": _role(actor)})
        return self.store.update("protocols", protocol_id, status="已提交")

    def safety_review_protocol(self, protocol_id, decision, comments="", actor=None):
        """安全委员会对方案版本放行/驳回。"""
        if _role(actor) != "安全委员会":
            raise TrialError(TrialErrorCode.FORBIDDEN, "只有安全委员会可以放行方案")
        protocol = self.store.require("protocols", protocol_id)
        if protocol["status"] != "已提交":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"方案当前为{protocol['status']}，不在待审状态")
        if decision not in ("放行", "驳回"):
            raise TrialError(TrialErrorCode.VALIDATION, "决定必须为 放行/驳回")
        self.store.create("releases", {
            "protocolId": protocol_id, "scope": "protocol",
            "decision": decision, "comments": comments, "by": _role(actor),
        })
        if decision == "放行":
            return self.store.update("protocols", protocol_id, status="安全放行")
        return self.store.update("protocols", protocol_id, status="草稿",
                                 lastReviewComments=comments)

    def activate_protocol(self, protocol_id, actor="试验协调员"):
        protocol = self.store.require("protocols", protocol_id)
        if protocol["status"] != "安全放行":
            raise TrialError(TrialErrorCode.SAFETY_GATE,
                             "安全委员会未放行的方案不能激活",
                             {"protocolId": protocol_id, "status": protocol["status"]})
        with self.store.lock:
            for other in self.store.list("protocols", status="已激活"):
                self.store.update("protocols", other["id"], status="已停用")
            return self.store.update("protocols", protocol_id, status="已激活")

    def deactivate_protocol(self, protocol_id, reason, actor="安全委员会"):
        protocol = self.store.require("protocols", protocol_id)
        if protocol["status"] != "已激活":
            raise TrialError(TrialErrorCode.INVALID_STATE, "仅已激活方案可停用")
        return self.store.update("protocols", protocol_id, status="已停用",
                                 deactivateReason=reason)

    def active_protocol(self):
        return self.store.find("protocols", status="已激活")

    # ------------------------------------------------------------------ #
    # 中心资质
    # ------------------------------------------------------------------ #
    def register_site(self, code, name, capabilities=None, actor="试验协调员"):
        if self.store.find("sites", code=code):
            raise TrialError(TrialErrorCode.DUPLICATE, f"中心编号已存在：{code}")
        return self.store.create("sites", {
            "code": code, "name": name,
            "capabilities": list(capabilities or []),
            "status": "待核查",
        })

    def _set_site_status(self, site_id, target, allowed, actor="试验协调员"):
        site = self.store.require("sites", site_id)
        if site["status"] not in allowed:
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"中心当前为{site['status']}，不能转为{target}")
        return self.store.update("sites", site_id, status=target)

    def activate_site(self, site_id, actor="试验协调员"):
        site = self.store.require("sites", site_id)
        if _REQUIRED_CAPABILITY not in site["capabilities"]:
            raise TrialError(TrialErrorCode.VALIDATION,
                             f"中心缺少资质能力：{_REQUIRED_CAPABILITY}")
        return self._set_site_status(site_id, "已激活", {"待核查", "已暂停"})

    def suspend_site(self, site_id, reason, actor="试验协调员"):
        return self._set_site_status(site_id, "已暂停", {"已激活"})

    def close_site(self, site_id, reason, actor="试验协调员"):
        return self._set_site_status(site_id, "已关闭", {"待核查", "已激活", "已暂停"})

    # ------------------------------------------------------------------ #
    # 物料与批次（光敏药物 / 球囊激光光纤器械）
    # ------------------------------------------------------------------ #
    def register_material(self, kind, code, name, manufacturer="", actor="试验协调员"):
        if kind not in catalog.DRUG_KIND:
            raise TrialError(TrialErrorCode.VALIDATION, "物料类别必须为 药物/器械")
        if self.store.find("materials", code=code):
            raise TrialError(TrialErrorCode.DUPLICATE, f"物料编号已存在：{code}")
        return self.store.create("materials", {
            "kind": kind, "code": code, "name": name,
            "manufacturer": manufacturer, "status": "在册",
        })

    def receive_batch(self, material_id, batch_no, expiry, quantity, actor="试验协调员"):
        material = self.store.require("materials", material_id)
        if self.store.find("batches", batchNo=batch_no, materialId=material_id):
            raise TrialError(TrialErrorCode.DUPLICATE, f"批次已存在：{batch_no}")
        return self.store.create("batches", {
            "materialId": material_id, "materialCode": material["code"],
            "kind": material["kind"], "batchNo": batch_no,
            "expiry": str(_date(expiry)), "quantity": quantity,
            "status": "可用",
        })

    def quarantine_batch(self, batch_id, reason, actor="试验协调员"):
        self.store.require("batches", batch_id)
        return self.store.update("batches", batch_id, status="隔离", holdReason=reason)

    def release_batch(self, batch_id, actor="试验协调员"):
        batch = self.store.require("batches", batch_id)
        if batch["status"] != "隔离":
            raise TrialError(TrialErrorCode.INVALID_STATE, "仅隔离批次可解除隔离")
        return self.store.update("batches", batch_id, status="可用", holdReason=None)

    def _usable_batch(self, batch_id, kind):
        batch = self.store.require("batches", batch_id)
        if batch["kind"] != kind:
            raise TrialError(TrialErrorCode.VALIDATION,
                             f"批次 {batch_id} 是{batch['kind']}，需要{kind}")
        if batch["status"] != "可用":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"批次 {batch_id} 状态为{batch['status']}，不能用于研究操作")
        if _date(batch["expiry"]) < self.store.today():
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"批次 {batch_id} 已过效期（{batch['expiry']}）")
        return batch

    # ------------------------------------------------------------------ #
    # 受试者、同意、入排
    # ------------------------------------------------------------------ #
    def register_subject(self, site_id, code, actor="研究者"):
        site = self.store.require("sites", site_id)
        if self.store.find("subjects", code=code):
            raise TrialError(TrialErrorCode.DUPLICATE, f"受试者编号已存在：{code}")
        return self.store.create("subjects", {
            "code": code, "siteId": site_id, "siteCode": site["code"],
            "status": "筛选中",
        })

    def sign_consent(self, subject_id, protocol_id, actor="研究者",
                     doc_ref=None, doc_checksum=None):
        subject = self.store.require("subjects", subject_id)
        if subject["status"] in ("已撤回", "筛选失败"):
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"受试者当前为{subject['status']}，不能签署同意")
        protocol = self.store.require("protocols", protocol_id)
        if protocol["status"] != "已激活":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             "只能对已激活方案版本签署知情同意",
                             {"protocolVersion": protocol["version"], "status": protocol["status"]})
        if doc_checksum is not None and not _SHA256.match(doc_checksum):
            raise TrialError(TrialErrorCode.VALIDATION, "同意书校验值必须为 64 位十六进制")
        with self.store.lock:
            for old in self.store.list("consents", subjectId=subject_id, superseded=False):
                self.store.update("consents", old["id"], superseded=True)
            return self.store.create("consents", {
                "subjectId": subject_id,
                "protocolId": protocol_id,
                "protocolVersion": protocol["version"],
                "docRef": doc_ref, "docChecksum": doc_checksum,
                "superseded": False, "by": _role(actor),
            })

    def _valid_consent(self, subject_id):
        consent = self.store.find("consents", subjectId=subject_id, superseded=False)
        if consent is None:
            raise TrialError(TrialErrorCode.VALIDATION, "缺少有效知情同意")
        return consent

    def record_eligibility(self, subject_id, inclusion_results, exclusion_results,
                           actor="研究者"):
        subject = self.store.require("subjects", subject_id)
        if subject["status"] not in ("筛选中", "已入组"):
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"受试者当前为{subject['status']}，不能登记入排")
        if not inclusion_results:
            raise TrialError(TrialErrorCode.VALIDATION, "必须逐条记录入选标准判定")
        failed_in = [k for k, v in inclusion_results.items() if not v]
        hit_ex = [k for k, v in exclusion_results.items() if v]
        decision = "符合" if not failed_in and not hit_ex else "不符合"
        record = self.store.create("eligibility", {
            "subjectId": subject_id,
            "inclusion": dict(inclusion_results),
            "exclusion": dict(exclusion_results),
            "decision": decision,
            "failedInclusion": failed_in,
            "hitExclusion": hit_ex,
            "by": _role(actor),
        })
        if decision == "不符合" and subject["status"] == "筛选中":
            self.store.update("subjects", subject_id, status="筛选失败")
        return record

    def enroll_subject(self, subject_id, actor="研究者"):
        subject = self.store.require("subjects", subject_id)
        if subject["status"] != "筛选中":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"受试者当前为{subject['status']}，不能入组")
        site = self.store.require("sites", subject["siteId"])
        if site["status"] != "已激活":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"中心状态为{site['status']}，不能入组受试者")
        if _REQUIRED_CAPABILITY not in site["capabilities"]:
            raise TrialError(TrialErrorCode.VALIDATION,
                             f"中心缺少资质能力：{_REQUIRED_CAPABILITY}")
        consent = self._valid_consent(subject_id)
        protocol = self.store.require("protocols", consent["protocolId"])
        if protocol["status"] != "已激活":
            raise TrialError(TrialErrorCode.VERSION_CONFLICT,
                             f"同意所依据的方案版本 {protocol['version']} 已非激活，"
                             "须按新版本重新签署同意",
                             {"consentVersion": protocol["version"]})
        eligibility = self.store.find("eligibility", subjectId=subject_id)
        if eligibility is None or eligibility["decision"] != "符合":
            raise TrialError(TrialErrorCode.VALIDATION, "入排判定未完成或结论不是符合")
        with self.store.lock:
            consent_at = _dt(consent["createdAt"])
            enroll_at = self.store.now()
            if enroll_at <= consent_at:
                enroll_at = consent_at + datetime.timedelta(minutes=1)
            self.store.create("timeline", {
                "subjectId": subject_id, "node": "同意",
                "at": consent_at.isoformat(timespec="minutes"), "by": consent["by"],
            })
            self.store.create("timeline", {
                "subjectId": subject_id, "node": "入组",
                "at": enroll_at.isoformat(timespec="minutes"),
                "by": _role(actor),
            })
            return self.store.update("subjects", subject_id, status="已入组",
                                     protocolId=protocol["id"],
                                     protocolVersion=protocol["version"])

    # ------------------------------------------------------------------ #
    # 剂量队列与安全放行
    # ------------------------------------------------------------------ #
    def define_cohort(self, protocol_id, name, level, drug_dose, light_regimen,
                      target_size, actor="研究者"):
        protocol = self.store.require("protocols", protocol_id)
        if drug_dose not in protocol["drugLevels"]:
            raise TrialError(TrialErrorCode.VALIDATION,
                             f"剂量 {drug_dose} 不在方案 {protocol['version']} 声明内")
        if light_regimen not in protocol["lightRegimens"]:
            raise TrialError(TrialErrorCode.VALIDATION,
                             f"照光方案 {light_regimen} 不在方案 {protocol['version']} 声明内")
        if self.store.find("cohorts", protocolId=protocol_id, level=level):
            raise TrialError(TrialErrorCode.DUPLICATE, f"剂量水平 {level} 队列已定义")
        return self.store.create("cohorts", {
            "protocolId": protocol_id, "protocolVersion": protocol["version"],
            "name": name, "level": level,
            "drugDose": drug_dose, "lightRegimen": light_regimen,
            "targetSize": target_size, "enrolled": 0,
            "status": "待启动", "frozen": False,
        })

    def _no_unreviewed_sae(self, exclude_event_id=None):
        for event in self.store.list("events", category="严重AE"):
            if event.get("disposition") is None and event["id"] != exclude_event_id:
                raise TrialError(TrialErrorCode.SAFETY_GATE,
                                 "存在尚未经安全委员会处置的严重不良事件，队列递进冻结",
                                 {"eventId": event["id"], "subjectId": event["subjectId"]})

    def submit_cohort(self, cohort_id, actor="研究者"):
        cohort = self.store.require("cohorts", cohort_id)
        if cohort["status"] != "待启动":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"队列当前为{cohort['status']}，不能提交放行")
        self._no_unreviewed_sae()
        # 递进规则：紧邻的低水平队列必须已开放，且至少一例受试者完成两周观察
        # （达到可评估）——爬坡必须有低水平安全数据支撑；低水平队列尚未
        # 定义同样阻止（不能跳过爬坡顺序）。
        prior = None
        for lower in self.store.list("cohorts", protocolId=cohort["protocolId"]):
            if lower["level"] < cohort["level"] and (
                    prior is None or lower["level"] > prior["level"]):
                prior = lower
        if prior is None:
            if cohort["level"] > 1:
                raise TrialError(TrialErrorCode.SAFETY_GATE,
                                 f"水平 {cohort['level'] - 1} 队列尚未建立，不能越级递进")
        elif prior["status"] not in ("开放", "暂停", "已关闭"):
            raise TrialError(TrialErrorCode.SAFETY_GATE,
                             f"低水平队列 {prior['name']} 尚未开放，不能递进",
                             {"lowerCohort": prior["id"]})
        if prior is not None:
            prior_assignments = self.store.list("assignments", cohortId=prior["id"])
            evaluable = any(
                self.store.get("subjects", a["subjectId"])
                and self.store.get("subjects", a["subjectId"])["status"] == "可评估"
                for a in prior_assignments)
            if not evaluable:
                raise TrialError(TrialErrorCode.SAFETY_GATE,
                                 f"低水平队列 {prior['name']} 尚无完成两周观察的受试者，"
                                 "缺少爬坡安全数据，不能递进",
                                 {"lowerCohort": prior["id"]})
        return self.store.update("cohorts", cohort_id, status="待安全放行")

    def safety_review_cohort(self, cohort_id, decision, comments="", actor=None):
        if _role(actor) != "安全委员会":
            raise TrialError(TrialErrorCode.FORBIDDEN, "只有安全委员会可以放行剂量队列")
        cohort = self.store.require("cohorts", cohort_id)
        if cohort["status"] != "待安全放行":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"队列当前为{cohort['status']}，不在待放行状态")
        if decision not in ("放行", "驳回"):
            raise TrialError(TrialErrorCode.VALIDATION, "决定必须为 放行/驳回")
        self.store.create("releases", {
            "protocolId": cohort["protocolId"], "scope": "cohort",
            "cohortId": cohort_id, "decision": decision,
            "comments": comments, "by": _role(actor),
        })
        if decision == "驳回":
            return self.store.update("cohorts", cohort_id, status="待启动")
        return cohort  # 放行后由 open_cohort 开放

    def _latest_release(self, cohort_id, scope="cohort"):
        releases = [r for r in self.store.list("releases", scope=scope,
                                               cohortId=cohort_id, decision="放行")]
        return releases[-1] if releases else None

    def open_cohort(self, cohort_id, actor="试验协调员"):
        cohort = self.store.require("cohorts", cohort_id)
        if self._latest_release(cohort_id) is None:
            raise TrialError(TrialErrorCode.SAFETY_GATE,
                             "安全委员会未放行该剂量队列")
        self._no_unreviewed_sae()
        if cohort["status"] != "待安全放行":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"队列当前为{cohort['status']}，不能开放")
        protocol = self.store.require("protocols", cohort["protocolId"])
        if protocol["status"] != "已激活":
            raise TrialError(TrialErrorCode.VERSION_CONFLICT,
                             "队列所属方案版本不是激活版本，不能开放入组")
        with self.store.lock:
            self.store.update("cohorts", cohort_id, status="开放", frozen=True)
            return self.store.get("cohorts", cohort_id)

    def close_cohort(self, cohort_id, reason, actor="安全委员会"):
        cohort = self.store.require("cohorts", cohort_id)
        if cohort["status"] == "已关闭":
            raise TrialError(TrialErrorCode.INVALID_STATE, "队列已关闭")
        return self.store.update("cohorts", cohort_id, status="已关闭",
                                 closeReason=reason)

    # ------------------------------------------------------------------ #
    # 队列分配（剂量混淆防护：一次分配、终身不变、快照冻结）
    # ------------------------------------------------------------------ #
    def assign_cohort(self, subject_id, cohort_id, drug_batch_id, device_batch_id,
                      actor="研究者"):
        if _role(actor) in catalog.BLINDED_ROLES:
            raise TrialError(TrialErrorCode.FORBIDDEN, "盲态角色不能接触或分配剂量队列")
        subject = self.store.require("subjects", subject_id)
        if self.store.find("assignments", subjectId=subject_id):
            raise TrialError(TrialErrorCode.CONFLICT, "受试者已有剂量分配，禁止重新分配（防剂量混淆）")
        if subject["status"] != "已入组":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"受试者当前为{subject['status']}，不能分配队列")
        cohort = self.store.require("cohorts", cohort_id)
        if cohort["status"] != "开放":
            raise TrialError(TrialErrorCode.SAFETY_GATE,
                             f"队列当前为{cohort['status']}，不能纳入受试者")
        consent = self._valid_consent(subject_id)
        if consent["protocolId"] != cohort["protocolId"]:
            raise TrialError(TrialErrorCode.VERSION_CONFLICT,
                             "队列所属方案版本与受试者同意版本不一致，错误方案已阻止",
                             {"consentProtocol": consent["protocolId"],
                              "cohortProtocol": cohort["protocolId"]})
        drug_batch = self._usable_batch(drug_batch_id, "药物")
        device_batch = self._usable_batch(device_batch_id, "器械")
        with self.store.lock:
            assignment = self.store.create("assignments", {
                "subjectId": subject_id,
                "cohortId": cohort_id,
                "cohortName": cohort["name"],
                "level": cohort["level"],
                "protocolId": cohort["protocolId"],
                "protocolVersion": cohort["protocolVersion"],
                "drugDose": cohort["drugDose"],
                "lightRegimen": cohort["lightRegimen"],
                "drugBatchId": drug_batch_id,
                "drugBatchNo": drug_batch["batchNo"],
                "deviceBatchId": device_batch_id,
                "deviceBatchNo": device_batch["batchNo"],
                "deviceChanges": [],
                "by": _role(actor),
            })
            self.store.update("cohorts", cohort_id, enrolled=cohort["enrolled"] + 1)
            self.store.update("subjects", subject_id, status="治疗中")
            return assignment

    def _assignment(self, subject_id):
        assignment = self.store.find("assignments", subjectId=subject_id)
        if assignment is None:
            raise TrialError(TrialErrorCode.INVALID_STATE, "受试者尚未分配剂量队列")
        return assignment

    # ------------------------------------------------------------------ #
    # 紧急偏离
    # ------------------------------------------------------------------ #
    def emergency_deviation(self, subject_id, summary, taken_by, actor="研究者",
                            at=None, category="操作偏离"):
        """先行处置登记：简要摘要必须当场记录，完整原因 24 小时内补录。"""
        subject = self.store.require("subjects", subject_id)
        if subject["status"] == "已撤回":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             "受试者已撤回，不能新增研究偏离记录")
        at = _dt(at) if at else self.store.now()
        return self.store.create("deviations", {
            "subjectId": subject_id, "category": category,
            "summary": summary, "takenBy": taken_by,
            "fullReason": None, "status": "待复核",
            "at": at.isoformat(timespec="minutes"),
            "dueAt": (at + datetime.timedelta(hours=catalog.DEVIATION_REPORT_HOURS))
            .isoformat(timespec="minutes"),
            "supplementedAt": None, "overdue": False,
            "reviewedAt": None, "reviewComments": None,
        })

    def supplement_deviation(self, deviation_id, full_reason, actor="研究者", at=None):
        deviation = self.store.require("deviations", deviation_id)
        if deviation["fullReason"] is not None:
            raise TrialError(TrialErrorCode.DUPLICATE, "偏离单已补录，不能重复补录")
        at = _dt(at) if at else self.store.now()
        overdue = at > _dt(deviation["dueAt"])
        changes = {
            "fullReason": full_reason,
            "supplementedAt": at.isoformat(timespec="minutes"),
            "overdue": overdue,
        }
        record = self.store.update("deviations", deviation_id, **changes)
        if overdue:
            self.store.log("deviation.overdue", deviation_id,
                           {"subjectId": deviation["subjectId"],
                            "hoursLate": round(_hours(at, deviation["dueAt"]), 1)})
        return record

    def review_deviation(self, deviation_id, decision, comments="", actor=None):
        if _role(actor) != "安全委员会":
            raise TrialError(TrialErrorCode.FORBIDDEN, "只有安全委员会可以复核偏离")
        deviation = self.store.require("deviations", deviation_id)
        if deviation["status"] != "待复核":
            raise TrialError(TrialErrorCode.INVALID_STATE, "偏离单已复核")
        if deviation["fullReason"] is None:
            raise TrialError(TrialErrorCode.OVERDUE, "偏离单尚未补录完整原因，不能复核")
        if decision not in ("确认", "驳回"):
            raise TrialError(TrialErrorCode.VALIDATION, "决定必须为 确认/驳回")
        new_status = "已确认" if decision == "确认" else "已驳回"
        return self.store.update("deviations", deviation_id, status=new_status,
                                 reviewedAt=self.store.now().isoformat(timespec="minutes"),
                                 reviewComments=comments, reviewedBy=_role(actor))

    def _usable_deviation(self, deviation_id, subject_id, at):
        if not deviation_id:
            return None
        deviation = self.store.require("deviations", deviation_id)
        if deviation["subjectId"] != subject_id:
            raise TrialError(TrialErrorCode.VALIDATION, "偏离单不属于该受试者")
        if deviation["status"] == "已驳回":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             "偏离已被安全委员会驳回，不能作为操作依据")
        if _dt(deviation["at"]) > _dt(at):
            raise TrialError(TrialErrorCode.VALIDATION, "偏离登记时间晚于操作时间")
        return deviation

    # ------------------------------------------------------------------ #
    # 治疗时间线（严格递增；实际必须与分配快照一致）
    # ------------------------------------------------------------------ #
    def _timeline(self, subject_id):
        return self.store.list("timeline", subjectId=subject_id)

    def _append_node(self, subject_id, node, at, actor, **extra):
        at = _dt(at)
        with self.store.lock:
            nodes = self._timeline(subject_id)
            if any(n["node"] == node for n in nodes):
                raise TrialError(TrialErrorCode.DUPLICATE, f"时间线节点 {node} 已存在")
            ordered = {n["node"]: n for n in nodes}
            last = nodes[-1] if nodes else None
            if last and at < _dt(last["at"]):
                raise TrialError(TrialErrorCode.VALIDATION,
                                 f"{node} 时间 {at:%Y-%m-%d %H:%M} 早于上一节点 "
                                 f"{last['node']}（{_dt(last['at']):%Y-%m-%d %H:%M}）")
            if last and at == _dt(last["at"]):
                raise TrialError(TrialErrorCode.VALIDATION,
                                 f"{node} 与 {last['node']} 时间相同，时间线须严格递增")
            expected_order = {v: i for i, v in enumerate(catalog.VISITS)}
            if last and expected_order[node] < expected_order[last["node"]]:
                raise TrialError(TrialErrorCode.VALIDATION,
                                 f"节点顺序错误：{node} 不能出现在 {last['node']} 之后")
            payload = {"subjectId": subject_id, "node": node,
                       "at": at.isoformat(timespec="minutes"), "by": _role(actor)}
            payload.update(extra)
            return self.store.create("timeline", payload)

    def _require_active_for_intervention(self, subject):
        if subject["status"] == "已撤回":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             "受试者已撤回：停止新增研究用途（治疗操作被阻止）")
        if subject["status"] != "治疗中":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"受试者当前为{subject['status']}，不能执行治疗操作")

    def administer_drug(self, subject_id, at, drug_batch_id=None, deviation_id=None,
                        actor="研究者"):
        subject = self.store.require("subjects", subject_id)
        self._require_active_for_intervention(subject)
        assignment = self._assignment(subject_id)
        at = _dt(at)
        batch_id = drug_batch_id or assignment["drugBatchId"]
        deviation = self._usable_deviation(deviation_id, subject_id, at)
        if batch_id != assignment["drugBatchId"] and deviation is None:
            raise TrialError(TrialErrorCode.CONFLICT,
                             "实际药物批次与分配批次不一致，且无紧急偏离单，操作已阻止")
        batch = self._usable_batch(batch_id, "药物")
        node = self._append_node(subject_id, "给药", at, actor,
                                 drugBatchId=batch_id, drugBatchNo=batch["batchNo"],
                                 drugDose=assignment["drugDose"],
                                 deviationId=deviation_id)
        return node

    def illuminate(self, subject_id, at, energy, duration_minutes,
                   device_batch_id=None, deviation_id=None, actor="研究者"):
        subject = self.store.require("subjects", subject_id)
        self._require_active_for_intervention(subject)
        assignment = self._assignment(subject_id)
        at = _dt(at)
        batch_id = device_batch_id or assignment["deviceBatchId"]
        deviation = self._usable_deviation(deviation_id, subject_id, at)
        if batch_id != assignment["deviceBatchId"] and deviation is None:
            raise TrialError(TrialErrorCode.CONFLICT,
                             "照射器械与分配器械不一致且无紧急偏离单，操作已阻止")
        self._usable_batch(batch_id, "器械")
        regimen = assignment["lightRegimen"]
        if (energy, duration_minutes) != (regimen["energy"], regimen["durationMinutes"]):
            if deviation is None:
                raise TrialError(TrialErrorCode.CONFLICT,
                                 "实际照光参数与队列方案不一致且无紧急偏离单，错误方案已阻止",
                                 {"planned": regimen,
                                  "actual": {"energy": energy,
                                             "durationMinutes": duration_minutes}})
        return self._append_node(subject_id, "照射", at, actor,
                                 deviceBatchId=batch_id,
                                 energy=energy, durationMinutes=duration_minutes,
                                 plannedRegimen=regimen, deviationId=deviation_id)

    def change_device(self, subject_id, new_device_batch_id, reason, at,
                      deviation_id=None, actor="研究者"):
        """术中/术前器械更换。

        照射尚未开始时可凭登记原因更换；照射已开始后的更换必须关联紧急偏离单。
        更换不改变队列、剂量与方案版本（错误方案在 illuminate 中继续被阻止）。
        """
        subject = self.store.require("subjects", subject_id)
        self._require_active_for_intervention(subject)
        assignment = self._assignment(subject_id)
        at = _dt(at)
        new_batch = self._usable_batch(new_device_batch_id, "器械")
        nodes = self._timeline(subject_id)
        illumination_started = any(n["node"] == "照射" for n in nodes)
        deviation = self._usable_deviation(deviation_id, subject_id, at)
        if illumination_started and deviation is None:
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             "照射已开始，器械更换必须关联紧急偏离单")
        if new_device_batch_id == assignment["deviceBatchId"]:
            raise TrialError(TrialErrorCode.VALIDATION, "新器械与当前器械相同，无需更换")
        with self.store.lock:
            change = {
                "fromBatchId": assignment["deviceBatchId"],
                "toBatchId": new_device_batch_id,
                "toBatchNo": new_batch["batchNo"],
                "reason": reason, "at": at.isoformat(timespec="minutes"),
                "deviationId": deviation_id, "by": _role(actor),
            }
            history = assignment["deviceChanges"] + [change]
            self.store.update("assignments", assignment["id"],
                              deviceBatchId=new_device_batch_id,
                              deviceBatchNo=new_batch["batchNo"],
                              deviceChanges=history)
            self.store.log("device.change", subject_id, change, actor=_role(actor))
            return change

    def postpone_procedure(self, subject_id, reason, at, new_planned_at=None,
                           actor="研究者"):
        """术期延后登记。观察窗始终按实际治疗结束日重算，故延后不会污染窗口。"""
        subject = self.store.require("subjects", subject_id)
        if subject["status"] not in ("已入组", "治疗中"):
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"受试者当前为{subject['status']}，不能登记术期延后")
        at = _dt(at)
        if self._timeline(subject_id):
            last = self._timeline(subject_id)[-1]
            if at < _dt(last["at"]):
                raise TrialError(TrialErrorCode.VALIDATION, "延后登记时间早于已发生节点")
        record = {
            "subjectId": subject_id, "reason": reason,
            "at": at.isoformat(timespec="minutes"),
            "newPlannedAt": _dt(new_planned_at).isoformat(timespec="minutes")
            if new_planned_at else None,
            "by": _role(actor),
        }
        self.store.log("procedure.postponed", subject_id, record, actor=_role(actor))
        return record

    def finish_treatment(self, subject_id, at, actor="研究者"):
        subject = self.store.require("subjects", subject_id)
        self._require_active_for_intervention(subject)
        self._append_node(subject_id, "治疗结束", at, actor)
        end_date = _date(at)
        start, end = catalog.observation_window(end_date)
        return self.store.update("subjects", subject_id, status="观察中",
                                 treatmentEnd=str(end_date),
                                 observationWindow={"start": str(start), "end": str(end)})

    # ------------------------------------------------------------------ #
    # 安全事件
    # ------------------------------------------------------------------ #
    def report_event(self, subject_id, category, at, description, reporter,
                     related=None, actor="研究者"):
        if category not in catalog.EVENT_CATEGORY:
            raise TrialError(TrialErrorCode.VALIDATION, "事件分类必须为 一般AE/严重AE")
        subject = self.store.require("subjects", subject_id)
        # 撤回后停止新增研究用途，但安全事件必须继续随访与记录（法规要求保留）。
        at = _dt(at)
        due = (at + datetime.timedelta(hours=catalog.SAE_REPORT_HOURS))
        event = self.store.create("events", {
            "subjectId": subject_id, "subjectCode": subject["code"],
            "category": category, "at": at.isoformat(timespec="minutes"),
            "description": description, "reporter": reporter,
            "related": related, "disposition": None,
            "dueAt": due.isoformat(timespec="minutes") if category == "严重AE" else None,
            "reportedToCommitteeAt": None, "reportOverdue": False,
        })
        if category == "严重AE":
            assignment = self.store.find("assignments", subjectId=subject_id)
            if assignment:
                cohort = self.store.require("cohorts", assignment["cohortId"])
                if cohort["status"] == "开放":
                    self.store.update("cohorts", cohort["id"], status="暂停",
                                      holdReason=f"SAE {event['id']} 待处置")
            self.store.log("safety.sae-reported", event["id"],
                           {"subjectId": subject_id, "cohortId":
                            assignment["cohortId"] if assignment else None})
        return event

    def notify_committee(self, event_id, at=None, actor="研究者"):
        """安全委员会报告动作（SAE 24 小时内）。"""
        event = self.store.require("events", event_id)
        if event["category"] != "严重AE":
            raise TrialError(TrialErrorCode.VALIDATION, "仅严重AE需要委员会报告")
        at = _dt(at) if at else self.store.now()
        overdue = at > _dt(event["dueAt"])
        return self.store.update("events", event_id,
                                 reportedToCommitteeAt=at.isoformat(timespec="minutes"),
                                 reportOverdue=overdue)

    def review_sae(self, event_id, decision, comments="", actor=None):
        """安全委员会处置 SAE：暂停 / 放行恢复 / 关闭队列。"""
        if _role(actor) != "安全委员会":
            raise TrialError(TrialErrorCode.FORBIDDEN, "只有安全委员会可以处置 SAE")
        event = self.store.require("events", event_id)
        if event["category"] != "严重AE":
            raise TrialError(TrialErrorCode.VALIDATION, "仅严重AE需要安全处置")
        if decision not in ("暂停", "放行", "关闭队列"):
            raise TrialError(TrialErrorCode.VALIDATION, "决定必须为 暂停/放行/关闭队列")
        assignment = self.store.find("assignments", subjectId=event["subjectId"])
        cohort_id = assignment["cohortId"] if assignment else None
        if decision == "放行":
            self._no_unreviewed_sae(exclude_event_id=event_id)
        with self.store.lock:
            updates = {"disposition": decision,
                       "reviewedAt": self.store.now().isoformat(timespec="minutes"),
                       "reviewComments": comments}
            self.store.update("events", event_id, **updates)
            if cohort_id and decision == "放行":
                cohort = self.store.require("cohorts", cohort_id)
                self.store.create("releases", {
                    "protocolId": cohort["protocolId"], "scope": "cohort",
                    "cohortId": cohort_id, "decision": "放行",
                    "comments": f"SAE {event_id} 处置后恢复", "by": _role(actor),
                })
                if cohort["status"] == "暂停":
                    self.store.update("cohorts", cohort_id, status="开放",
                                      holdReason=None)
            elif cohort_id and decision == "关闭队列":
                self.store.update("cohorts", cohort_id, status="已关闭",
                                  closeReason=f"SAE {event_id}")
            # 暂停：维持现状
            return self.store.get("events", event_id)

    # ------------------------------------------------------------------ #
    # 两周评估：去标识化引用 + 校验值 + 观察窗
    # ------------------------------------------------------------------ #
    def _window_of(self, subject):
        if not subject.get("observationWindow"):
            raise TrialError(TrialErrorCode.INVALID_STATE, "治疗尚未结束，观察窗未开始")
        return (datetime.date.fromisoformat(subject["observationWindow"]["start"]),
                datetime.date.fromisoformat(subject["observationWindow"]["end"]))

    def submit_evaluation_material(self, subject_id, kind, deidentified_ref, checksum,
                                   collected_at, actor="盲态评价者"):
        if kind not in ("影像", "病理"):
            raise TrialError(TrialErrorCode.VALIDATION, "材料类型必须为 影像/病理")
        if not deidentified_ref:
            raise TrialError(TrialErrorCode.VALIDATION, "必须提供去标识化引用")
        if not _SHA256.match(checksum or ""):
            raise TrialError(TrialErrorCode.VALIDATION, "校验值必须为 64 位十六进制 SHA-256")
        subject = self.store.require("subjects", subject_id)
        if subject["status"] == "已撤回":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             "受试者已撤回：停止新增研究用途（评估材料拒收）")
        if subject["status"] not in ("观察中", "可评估"):
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             f"受试者当前为{subject['status']}，不能提交评估材料")
        start, end = self._window_of(subject)
        collected = _date(collected_at)
        if not (start <= collected <= end):
            raise TrialError(TrialErrorCode.WINDOW_VIOLATION,
                             f"{kind}采集日 {collected} 超出观察窗 {start}~{end}，材料拒收",
                             {"collectedAt": str(collected),
                              "window": {"start": str(start), "end": str(end)}})
        existing = self.store.find("evaluations", subjectId=subject_id, kind=kind)
        if existing:
            raise TrialError(TrialErrorCode.DUPLICATE,
                             f"{kind}材料已提交；如需更正须走更正流程")
        return self.store.create("evaluations", {
            "subjectId": subject_id, "kind": kind,
            "deidentifiedRef": deidentified_ref,
            "checksum": checksum,
            "collectedAt": str(collected),
            "submittedBy": _role(actor),
            "verifiedAt": None,
        })

    def verify_checksum(self, subject_id, kind, checksum, actor="盲态评价者"):
        records = [e for e in self.store.list("evaluations", subjectId=subject_id, kind=kind)]
        if not records:
            raise TrialError(TrialErrorCode.NOT_FOUND, f"缺少{kind}材料")
        record = records[-1]
        if checksum != record["checksum"]:
            raise TrialError(TrialErrorCode.CHECKSUM_MISMATCH,
                             f"{kind}校验值与登记值不一致，材料完整性校验失败",
                             {"expected": record["checksum"], "received": checksum})
        if record["verifiedAt"] is None:
            self.store.update("evaluations", record["id"],
                              verifiedAt=self.store.now().isoformat(timespec="minutes"))
        return self.store.get("evaluations", record["id"])

    def evaluability(self, subject_id):
        """计算并落定可评估状态，返回 (subject, reasons)。"""
        subject = self.store.require("subjects", subject_id)
        reasons = []
        if subject["status"] == "已撤回":
            reasons.append("受试者已撤回")
        materials = self.store.list("evaluations", subjectId=subject_id)
        kinds = {m["kind"]: m for m in materials}
        for kind in ("影像", "病理"):
            material = kinds.get(kind)
            if material is None:
                reasons.append(f"缺少{kind}材料")
            elif material["verifiedAt"] is None:
                reasons.append(f"{kind}校验值未核验")
        if not subject.get("observationWindow"):
            reasons.append("治疗未结束")
        for deviation in self.store.list("deviations", subjectId=subject_id):
            if deviation["status"] == "已驳回":
                reasons.append(f"存在被驳回的偏离 {deviation['id']}")
            elif deviation["overdue"] and deviation["status"] == "待复核":
                reasons.append(f"偏离 {deviation['id']} 补录超期且未复核")
        evaluable = not reasons and subject["status"] in ("观察中", "可评估")
        if evaluable and subject["status"] == "观察中":
            existing = self.store.list("timeline", subjectId=subject_id, node="两周评估")
            if not existing:
                # 评估时间取窗内已核验材料的最晚采集日（真实评估发生时点）。
                collected = [
                    _dt(m["collectedAt"]) + datetime.timedelta(hours=12)
                    for m in self.store.list("evaluations", subjectId=subject_id)
                    if m.get("verifiedAt")
                ]
                nodes = self._timeline(subject_id)
                stamp = max(collected) if collected else self.store.now()
                if nodes and stamp <= _dt(nodes[-1]["at"]):
                    stamp = _dt(nodes[-1]["at"]) + datetime.timedelta(minutes=1)
                self.store.create("timeline", {
                    "subjectId": subject_id, "node": "两周评估",
                    "at": stamp.isoformat(timespec="minutes"), "by": "系统",
                    "basis": "观察窗内影像+病理校验通过",
                })
            subject = self.store.update("subjects", subject_id, status="可评估")
        return subject, reasons

    # ------------------------------------------------------------------ #
    # 结局与医学决定
    # ------------------------------------------------------------------ #
    def record_outcome(self, subject_id, result, decision, decided_by, at,
                       actor="研究者"):
        if result not in catalog.OUTCOME:
            raise TrialError(TrialErrorCode.VALIDATION, "结局必须为 可切除/不可切除/待定")
        subject = self.store.require("subjects", subject_id)
        if subject["status"] == "已撤回":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             "受试者已撤回：不能新增研究结局判定")
        subject, reasons = self.evaluability(subject_id)
        if subject["status"] != "可评估":
            raise TrialError(TrialErrorCode.INVALID_STATE,
                             "受试者尚不可评估，不能登记结局", {"reasons": reasons})
        if self.store.find("outcomes", subjectId=subject_id):
            raise TrialError(TrialErrorCode.DUPLICATE, "结局已登记")
        provenance = self.provenance(subject_id)
        return self.store.create("outcomes", {
            "subjectId": subject_id,
            "result": result,
            "medicalDecision": decision,
            "decidedBy": decided_by,
            "at": _dt(at).isoformat(timespec="minutes"),
            "provenance": {
                "protocolId": provenance["protocol"]["id"],
                "protocolVersion": provenance["protocol"]["version"],
                "cohortId": provenance["assignment"]["cohortId"],
                "drugDose": provenance["assignment"]["drugDose"],
                "lightRegimen": provenance["assignment"]["lightRegimen"],
                "drugBatchNo": provenance["assignment"]["drugBatchNo"],
                "deviceBatchNo": provenance["assignment"]["deviceBatchNo"],
                "deviceChanges": len(provenance["assignment"]["deviceChanges"]),
                "timeline": [(n["node"], n["at"]) for n in provenance["timeline"]],
                "deviations": [d["id"] for d in provenance["deviations"]],
                "events": [e["id"] for e in provenance["events"]],
            },
        })

    # ------------------------------------------------------------------ #
    # 撤回
    # ------------------------------------------------------------------ #
    def withdraw_subject(self, subject_id, reason, at=None, actor="受试者"):
        subject = self.store.require("subjects", subject_id)
        if subject["status"] == "已撤回":
            raise TrialError(TrialErrorCode.DUPLICATE, "受试者已撤回")
        at = _dt(at) if at else self.store.now()
        self.store.create("withdrawals", {
            "subjectId": subject_id, "reason": reason,
            "at": at.isoformat(timespec="minutes"), "by": _role(actor),
        })
        # 安全记录（AE/SAE）保留：不删除任何既有表记录
        return self.store.update("subjects", subject_id, status="已撤回",
                                 withdrawnAt=at.isoformat(timespec="minutes"),
                                 withdrawReason=reason)

    # ------------------------------------------------------------------ #
    # 溯源
    # ------------------------------------------------------------------ #
    def provenance(self, subject_id):
        subject = self.store.require("subjects", subject_id)
        consent = self.store.find("consents", subjectId=subject_id, superseded=False)
        protocol = self.store.get("protocols", consent["protocolId"]) if consent else None
        assignment = self.store.find("assignments", subjectId=subject_id)
        eligibility = self.store.find("eligibility", subjectId=subject_id)
        outcome = self.store.find("outcomes", subjectId=subject_id)
        return {
            "subject": subject,
            "consent": consent,
            "protocol": protocol,
            "eligibility": eligibility,
            "assignment": assignment,
            "timeline": self._timeline(subject_id),
            "deviations": self.store.list("deviations", subjectId=subject_id),
            "events": self.store.list("events", subjectId=subject_id),
            "evaluations": self.store.list("evaluations", subjectId=subject_id),
            "outcome": outcome,
        }

    def audit_trail(self, target=None):
        return self.store.audit_trail(target)

    # ------------------------------------------------------------------ #
    # 角色视图（盲态遮蔽）
    # ------------------------------------------------------------------ #
    _BLINDED_HIDDEN_FIELDS = (
        "cohortId", "cohortName", "level", "protocolId", "protocolVersion",
        "drugDose", "lightRegimen", "drugBatchId", "drugBatchNo",
        "deviceBatchId", "deviceBatchNo", "deviceChanges",
    )

    def subject_view(self, subject_id, role):
        data = self.provenance(subject_id)
        if not catalog.is_blinded_role(role):
            data["blinded"] = False
            return data
        subject = data["subject"]
        # 盲态可见：受试者状态/观察窗、治疗结束节点、去标识化评估材料。
        # 队列、剂量、方案版本、批次、器械、偏离与安全事件细节一律遮蔽。
        visible_subject = {
            k: v for k, v in subject.items()
            if k in ("id", "code", "siteId", "siteCode", "status",
                     "treatmentEnd", "observationWindow",
                     "createdAt", "updatedAt")
        }
        timeline = [dict(n) for n in data["timeline"] if n["node"] == "治疗结束"]
        return {
            "subject": visible_subject,
            "consent": None,
            "protocol": None,
            "eligibility": None,
            "assignment": None,
            "timeline": timeline,
            "deviations": None,
            "events": None,
            "evaluations": data["evaluations"],
            "outcome": None,
            "blinded": True,
        }

    def list_subjects_view(self, role):
        subjects = self.store.list("subjects")
        if not catalog.is_blinded_role(role):
            return subjects
        hidden = {"cohortId", "protocolId", "protocolVersion"}
        return [{k: v for k, v in s.items() if k not in hidden} for s in subjects]
