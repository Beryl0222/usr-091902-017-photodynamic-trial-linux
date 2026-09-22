"""受控流程的规则冲突类型。

任何违反受控流程的操作都抛出 ``TrialError``，由接口层映射为
4xx 响应；规则冲突不产生任何业务记录（原子拒绝）。
"""


class TrialErrorCode:
    NOT_FOUND = "NOT_FOUND"
    INVALID_STATE = "INVALID_STATE"
    VALIDATION = "VALIDATION"
    CONFLICT = "CONFLICT"
    FORBIDDEN = "FORBIDDEN"          # 角色无权或盲态遮蔽
    SAFETY_GATE = "SAFETY_GATE"      # 安全委员会未放行
    WINDOW_VIOLATION = "WINDOW_VIOLATION"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    OVERDUE = "OVERDUE"              # 紧急偏离/SAE 补录超期
    DUPLICATE = "DUPLICATE"
    VERSION_CONFLICT = "VERSION_CONFLICT"


class TrialError(Exception):
    """业务规则冲突。``code`` 用于接口层状态码映射与测试断言。"""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_payload(self):
        return {"error": self.code, "message": self.message, "details": self.details}
