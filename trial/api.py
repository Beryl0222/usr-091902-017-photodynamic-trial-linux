"""HTTP 命令分发：把 JSON 请求映射到 TrialCoordinator 的领域命令。

* POST /api/commands/<命令名>，请求体即命令参数（``role`` 表示操作者角色）。
* GET  /api/<资源> 查询；GET /api/subjects/<id>?role= 返回角色视图
  （盲态角色自动遮蔽队列/剂量/批次/器械信息）。

这一层不含业务规则，只做参数透传与 ``TrialError`` -> HTTP 状态码映射。
"""

import inspect
import json
from urllib.parse import urlparse, parse_qs

from trial.errors import TrialError, TrialErrorCode
from trial.store import EventStore
from trial.coordinator import TrialCoordinator

# 命令名 -> coordinator 方法名
COMMANDS = {
    # 方案版本
    "draft_protocol": "draft_protocol",
    "submit_protocol": "submit_protocol",
    "review_protocol": "safety_review_protocol",
    "activate_protocol": "activate_protocol",
    "deactivate_protocol": "deactivate_protocol",
    # 中心
    "register_site": "register_site",
    "activate_site": "activate_site",
    "suspend_site": "suspend_site",
    "close_site": "close_site",
    # 物料批次
    "register_material": "register_material",
    "receive_batch": "receive_batch",
    "quarantine_batch": "quarantine_batch",
    "release_batch": "release_batch",
    # 受试者 / 同意 / 入排
    "register_subject": "register_subject",
    "sign_consent": "sign_consent",
    "record_eligibility": "record_eligibility",
    "enroll_subject": "enroll_subject",
    # 队列
    "define_cohort": "define_cohort",
    "submit_cohort": "submit_cohort",
    "review_cohort": "safety_review_cohort",
    "open_cohort": "open_cohort",
    "close_cohort": "close_cohort",
    "assign_cohort": "assign_cohort",
    # 偏离
    "emergency_deviation": "emergency_deviation",
    "supplement_deviation": "supplement_deviation",
    "review_deviation": "review_deviation",
    # 治疗执行
    "administer_drug": "administer_drug",
    "illuminate": "illuminate",
    "change_device": "change_device",
    "postpone_procedure": "postpone_procedure",
    "finish_treatment": "finish_treatment",
    # 安全
    "report_event": "report_event",
    "notify_committee": "notify_committee",
    "review_sae": "review_sae",
    # 评估与结局
    "submit_material": "submit_evaluation_material",
    "verify_checksum": "verify_checksum",
    "evaluability": "evaluability",
    "record_outcome": "record_outcome",
    # 撤回
    "withdraw_subject": "withdraw_subject",
}

_STATUS_CODES = {
    TrialErrorCode.NOT_FOUND: 404,
    TrialErrorCode.FORBIDDEN: 403,
    TrialErrorCode.VALIDATION: 400,
    TrialErrorCode.CHECKSUM_MISMATCH: 422,
    TrialErrorCode.INVALID_STATE: 409,
    TrialErrorCode.CONFLICT: 409,
    TrialErrorCode.DUPLICATE: 409,
    TrialErrorCode.VERSION_CONFLICT: 409,
    TrialErrorCode.SAFETY_GATE: 409,
    TrialErrorCode.WINDOW_VIOLATION: 409,
    TrialErrorCode.OVERDUE: 409,
}

def _accepts_actor(method):
    return "actor" in inspect.signature(method).parameters


COLLECTIONS = {
    "protocols": "protocols",
    "sites": "sites",
    "subjects": "subjects",
    "materials": "materials",
    "batches": "batches",
    "cohorts": "cohorts",
    "consents": "consents",
    "assignments": "assignments",
    "deviations": "deviations",
    "events": "events",
    "evaluations": "evaluations",
    "outcomes": "outcomes",
    "timeline": "timeline",
}


class Api:
    """无状态分发器；一个进程一个共享 coordinator（带锁，线程安全）。"""

    def __init__(self, coordinator=None):
        self.coordinator = coordinator or TrialCoordinator(EventStore())

    def dispatch_command(self, name, payload):
        method_name = COMMANDS.get(name)
        if method_name is None:
            raise TrialError(TrialErrorCode.NOT_FOUND, f"未知命令：{name}")
        if not isinstance(payload, dict):
            raise TrialError(TrialErrorCode.VALIDATION, "请求体必须是 JSON 对象")
        kwargs = dict(payload)
        actor = self._actor(kwargs)
        method = getattr(self.coordinator, method_name)
        if actor is not None and _accepts_actor(method):
            kwargs["actor"] = actor
        return method(**kwargs)

    @staticmethod
    def _actor(kwargs):
        role = kwargs.pop("role", None)
        if role is None:
            return None
        return {"role": role, "id": kwargs.pop("actorId", None)}

    def dispatch_get(self, path, query):
        parts = [p for p in path.split("/") if p]
        if len(parts) == 2 and parts[0] == "subjects":
            role = (query.get("role") or [None])[0]
            return self.coordinator.subject_view(parts[1], role or "研究者")
        if len(parts) == 2 and parts[0] == "provenance":
            return self.coordinator.provenance(parts[1])
        if len(parts) == 3 and parts[0] == "subjects" and parts[2] == "window":
            subject = self.coordinator.store.require("subjects", parts[1])
            return {
                "subjectId": subject["id"],
                "status": subject["status"],
                "treatmentEnd": subject.get("treatmentEnd"),
                "observationWindow": subject.get("observationWindow"),
            }
        if len(parts) == 2 and parts[0] == "audit":
            return self.coordinator.audit_trail(parts[1] or None)
        if len(parts) == 1 and parts[0] == "audit":
            return self.coordinator.audit_trail()
        if len(parts) == 1 and parts[0] in COLLECTIONS:
            return self.coordinator.store.list(COLLECTIONS[parts[0]])
        raise TrialError(TrialErrorCode.NOT_FOUND, f"未知查询路径：/{path}")


def make_handler(api=None):
    """生成绑定指定 Api 的 HTTP Handler 类（便于测试注入）。"""
    from http.server import BaseHTTPRequestHandler

    class ApiHandler(BaseHTTPRequestHandler):
        _api = api or Api()

        def _write(self, status, value):
            body = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                from service import health_payload
                self._write(200, health_payload())
                return
            if not parsed.path.startswith("/api/"):
                self.send_error(404)
                return
            try:
                result = self._api.dispatch_get(
                    parsed.path[len("/api/"):], parse_qs(parsed.query))
            except TrialError as error:
                self._write(_STATUS_CODES[error.code], error.to_payload())
            except TypeError as error:
                self._write(400, {"error": "VALIDATION", "message": str(error)})
            else:
                self._write(200, result)

        def do_POST(self):
            parsed = urlparse(self.path)
            prefix = "/api/commands/"
            if not parsed.path.startswith(prefix):
                self.send_error(404)
                return
            name = parsed.path[len(prefix):]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
                result = self._api.dispatch_command(name, payload)
            except json.JSONDecodeError:
                self._write(400, {"error": "VALIDATION", "message": "请求体不是合法 JSON"})
            except TrialError as error:
                self._write(_STATUS_CODES[error.code], error.to_payload())
            except TypeError as error:
                self._write(400, {"error": "VALIDATION", "message": str(error)})
            else:
                self._write(201, result)

        def log_message(self, *_args):
            return

    return ApiHandler
