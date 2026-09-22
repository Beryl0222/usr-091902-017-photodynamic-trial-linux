"""领域词表与受控流程常量。

只放词表与纯函数，不放业务规则（业务规则在 coordinator 中）。
"""

# 系统角色
ROLES = ("安全委员会", "研究者", "试验协调员", "盲态评价者", "受试者")

# 需要对剂量队列信息保持盲态的角色
BLINDED_ROLES = frozenset({"盲态评价者"})

# 方案版本状态机：草稿 -> 已提交 -> 安全放行 -> 激活（可被新版本取代）
PROTOCOL_STATUS = ("草稿", "已提交", "安全放行", "已激活", "已停用")

# 中心资质状态
SITE_STATUS = ("待核查", "已激活", "已暂停", "已关闭")

# 受试者状态机
SUBJECT_STATUS = (
    "筛选中",   # 已建档，等待入排判定
    "已入组",   # 符合条件 + 有效同意 + 活跃方案 + 中心激活
    "治疗中",   # 已分配队列、开始给药/照射
    "观察中",   # 治疗结束，两周观察期内
    "可评估",   # 观察窗内完成影像+病理
    "已撤回",   # 受试者撤回：停止新增研究用途
    "筛选失败",  # 不符合入排
)

# 队列进度（仅非盲角色可见）
COHORT_STATUS = ("待启动", "待安全放行", "开放", "暂停", "已关闭")

# 紧急偏离状态
DEVIATION_STATUS = ("待复核", "已确认", "已驳回")

# 去标识化评估材料状态
EVALUATION_STATUS = ("待影像", "待病理", "可评估", "材料被拒")

# 不良事件分类
EVENT_CATEGORY = ("一般AE", "严重AE")

# 物料类别
DRUG_KIND = ("药物", "器械")

# 结局（由非盲医学决定登记）
OUTCOME = ("可切除", "不可切除", "待定")

# 操作时间线节点（按顺序）
VISITS = ("同意", "入组", "给药", "照射", "治疗结束", "两周评估")

# ---- 受控时限 ----------------------------------------------------------

# 紧急偏离先行处置后，必须补录原因的时限（小时）
DEVIATION_REPORT_HOURS = 24

# 安全事件升级为 SAE 后，向安全委员会报告的时限（小时）
SAE_REPORT_HOURS = 24

# 两周观察窗：第14天 ± 2 天
OBSERVATION_DAY = 14
WINDOW_BEFORE = 2
WINDOW_AFTER = 2


def observation_window(treatment_end):
    """根据治疗结束日期返回观察窗起止日期（含端点）。"""
    import datetime

    if isinstance(treatment_end, str):
        treatment_end = datetime.date.fromisoformat(treatment_end)
    start = treatment_end + datetime.timedelta(days=OBSERVATION_DAY - WINDOW_BEFORE)
    end = treatment_end + datetime.timedelta(days=OBSERVATION_DAY + WINDOW_AFTER)
    return start, end


def is_blinded_role(role):
    """角色是否处于盲态，不能接触队列/剂量/批次/器械信息。"""
    return role in BLINDED_ROLES
