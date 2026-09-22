"""存储与审计基础设施（默认内存实现，便于测试）。

所有业务变更都经过 EventStore 记录不可变审计事件；
时钟与 ID 生成器可注入，便于把六名受试者并发场景做成确定性回放。
"""

import datetime
import itertools
import threading
from copy import deepcopy


def fixed_clock(start="2026-01-05T08:00:00", step_minutes=0):
    """返回一个可推进的时钟。

    step_minutes>0 时每次调用自动前进，用于并发回放；
    也可显式 clock(now="...") 跳到指定时刻。
    """
    current = [datetime.datetime.fromisoformat(start)]
    step = datetime.timedelta(minutes=step_minutes)

    def clock(now=None):
        if now is not None:
            current[0] = datetime.datetime.fromisoformat(now) if isinstance(now, str) else now
            return current[0]
        value = current[0]
        if step:
            current[0] = current[0] + step
        return value

    return clock


def system_clock():
    return datetime.datetime.now


def seq_id(prefix):
    counter = itertools.count(1)

    def gen():
        return f"{prefix}-{next(counter):04d}"

    return gen


class EventStore:
    """线程安全的内存表存储 + 只追加审计日志。"""

    TABLES = (
        "protocols", "submissions", "releases",
        "sites", "subjects", "consents", "eligibility",
        "materials", "batches", "cohorts", "assignments",
        "timeline", "deviations", "events",
        "evaluations", "outcomes", "withdrawals",
    )

    def __init__(self, clock=None):
        self._lock = threading.RLock()
        self.clock = clock or system_clock()
        self.tables = {name: {} for name in self.TABLES}
        self.audit = []
        self.id_generators = {
            "protocols": seq_id("PR"),
            "submissions": seq_id("SM"),
            "releases": seq_id("RL"),
            "sites": seq_id("ST"),
            "subjects": seq_id("SB"),
            "consents": seq_id("IC"),
            "eligibility": seq_id("EL"),
            "materials": seq_id("MT"),
            "batches": seq_id("BT"),
            "cohorts": seq_id("CH"),
            "assignments": seq_id("AS"),
            "timeline": seq_id("TL"),
            "deviations": seq_id("DV"),
            "events": seq_id("AE"),
            "evaluations": seq_id("EV"),
            "outcomes": seq_id("OC"),
            "withdrawals": seq_id("WD"),
        }

    @property
    def lock(self):
        return self._lock

    def now(self):
        return self.clock()

    def today(self):
        return self.clock().date()

    def create(self, table, payload):
        """插入一条记录并写审计，返回带 id/createdAt 的副本。"""
        with self._lock:
            record = deepcopy(payload)
            record["id"] = payload.get("id") or self.id_generators[table]()
            record["createdAt"] = self.now().isoformat(timespec="minutes")
            self.tables[table][record["id"]] = record
            self._audit(table + ".create", record["id"], record)
            return deepcopy(record)

    def update(self, table, record_id, **changes):
        """局部更新并写审计（旧值记入审计细节）。"""
        with self._lock:
            record = self.tables[table].get(record_id)
            if record is None:
                raise KeyError(record_id)
            before = deepcopy(record)
            record.update(deepcopy(changes))
            record["updatedAt"] = self.now().isoformat(timespec="minutes")
            self._audit(table + ".update", record_id, {"before": before, "after": deepcopy(record)})
            return deepcopy(record)

    def get(self, table, record_id):
        with self._lock:
            record = self.tables[table].get(record_id)
            return deepcopy(record) if record else None

    def require(self, table, record_id):
        record = self.get(table, record_id)
        if record is None:
            from trial.errors import TrialError, TrialErrorCode
            raise TrialError(
                TrialErrorCode.NOT_FOUND,
                f"{table[:-1]} 不存在：{record_id}",
                {"table": table, "id": record_id},
            )
        return record

    def list(self, table, **filters):
        with self._lock:
            items = self.tables[table].values()
            result = [deepcopy(r) for r in items]
        if filters:
            def match(record):
                return all(record.get(k) == v for k, v in filters.items())
            result = [r for r in result if match(r)]
        return sorted(result, key=lambda r: r.get("createdAt", ""))

    def find(self, table, **filters):
        matches = self.list(table, **filters)
        return matches[-1] if matches else None

    def log(self, action, target, details=None, actor=None):
        """写一条纯领域审计（不改变任何表）。"""
        with self._lock:
            self._audit(action, target, details, actor)

    def _audit(self, action, target, details, actor=None):
        self.audit.append({
            "seq": len(self.audit) + 1,
            "at": self.now().isoformat(timespec="minutes"),
            "actor": actor,
            "action": action,
            "target": target,
            "details": deepcopy(details) if details is not None else None,
        })

    def audit_trail(self, target=None):
        with self._lock:
            trail = [deepcopy(e) for e in self.audit if target is None or e["target"] == target]
        # 目标相关事件也可能记录在 details 中（溯源链按 details 反查）
        if target is not None:
            linked = [
                deepcopy(e) for e in self.audit
                if e["target"] != target and _details_mentions(e["details"], target)
            ]
            seen = {e["seq"] for e in trail}
            trail.extend(e for e in linked if e["seq"] not in seen)
            trail.sort(key=lambda e: e["seq"])
        return trail


def _details_mentions(details, target):
    if isinstance(details, dict):
        if target in details.values():
            return True
        return any(_details_mentions(v, target) for v in details.values())
    if isinstance(details, list):
        return any(_details_mentions(v, target) for v in details)
    return False


# 向后兼容别名
MemoryStore = EventStore
