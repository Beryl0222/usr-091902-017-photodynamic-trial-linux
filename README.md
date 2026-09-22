# 光动力早期试验

管理早期临床方案版本、中心资质、受试者同意、入排条件、药物与器械批次、剂量队列及
操作时间线的**受控流程**服务。光敏药物注射 + 血管内球囊激光光纤照射，两周后评估
残余肿瘤可否切除；病例少，任何越窗、剂量混淆或安全事件漏报都会让结论失真。

## 受控规则（核心）

- **方案版本闸门**：草稿 → 已提交 → **安全委员会放行** → 已激活；新版本激活自动
  停用旧版本。只能对激活版本签同意；同意所依版本被取代后必须重新签署才能入组。
- **中心资质**：缺少"光动力介入"能力不能激活；暂停/关闭中心不能入组。
- **批次受控**：药物/器械批次须在册、可用、未过效期；隔离批次不能用于任何操作。
- **剂量队列**：队列绑定方案版本、剂量与照光参数；开放入组必须有安全委员会放行；
  爬坡到下一水平前，低水平队列必须已开放且**至少一例完成两周观察（可评估）**；
  存在未处置 SAE 时所有队列递进/再开放冻结，恢复须安全委员会再放行。
- **剂量混淆防护**：队列分配一次完成、终身不可改；分配时冻结方案版本/剂量/
  照光参数/药批/械批快照。盲态角色不能接触或分配队列。
- **操作时间线**：同意→入组→给药→照射→治疗结束→两周评估严格递增。实际药批、
  器械、照光能量/时长必须与分配快照一致；不一致时**原子拒绝**（不落任何记录），
  只有关联**紧急偏离单**才允许例外。
- **器械更换**：照射前登记原因即可；照射开始后必须关联紧急偏离单。更换器械不改
  方案，错误照光参数仍被阻止；更换历史进入结局溯源。
- **术期延后**：仅登记原因与新计划时间；两周观察窗始终按**实际治疗结束日**重算
  （第 14 天 ± 2 天），延后不会污染窗口。
- **紧急偏离**：可先行处置并当场登记摘要，**24 小时内补录完整原因**（超期标记），
  安全委员会复核确认/驳回；被驳回的偏离不能再作为任何操作的依据。
- **安全事件**：SAE 报告后自动暂停所在队列；24 小时内须报告安全委员会；委员会
  可决定暂停/放行恢复/关闭队列。撤回后仍可继续随访记录安全事件。
- **两周评估**：影像/病理只存**去标识化引用 + SHA-256 校验值**；采集日落在
  观察窗外一律拒收（窗两端含端点）；校验不一致拒绝；双材料核验通过方可评估。
- **撤回**：受试者撤回后停止一切新增研究用途（治疗/材料/结局），但既有同意、
  分配、时间线与法规要求的安全记录全部保留。
- **溯源**：每个结局固化获批方案版本、实际剂量、照光参数、药物批次、器械批次
  （含更换次数）、完整时间线、偏离单与安全事件、医学决定与决定人；所有变更写
  只追加审计日志。
- **盲态视图**：`盲态评价者` 只能看到受试者状态、观察窗、治疗结束节点与
  去标识化评估材料；队列、剂量、方案版本、批次、器械、偏离与事件全部遮蔽。

## 代码结构

```
service.py              运行入口（/health 与 /api 挂载）
service_contract.py     HTTP 契约测试
test_domain.py          领域规则单元测试（31 例）
scenario.py             六名受试者并发安排端到端场景（43 项断言）
trial/
  catalog.py            词表、状态、观察窗/时限常量与盲态角色判定
  errors.py             规则冲突类型（映射 HTTP 4xx）
  store.py              线程安全内存表 + 只追加审计日志（时钟/ID 可注入）
  coordinator.py        受控流程核心引擎（全部业务规则）
  api.py                HTTP 命令分发（POST /api/commands/<命令>）
fixtures/domain.json    领域词表
```

## 运行

```bash
python3 service.py --check     # 基础配置检查
python3 service.py --port 8000 # 启动服务
npm test                       # 单元测试 + 契约测试 + 六人场景
```

## HTTP 接口

健康检查：

```
GET /health -> {"status":"ok","service":"photodynamic-trial","name":"光动力早期试验"}
```

命令（请求体为 JSON 参数，`role` 表示操作者角色）：

```
POST /api/commands/draft_protocol
POST /api/commands/submit_protocol
POST /api/commands/review_protocol        # role=安全委员会, decision=放行/驳回
POST /api/commands/activate_protocol
POST /api/commands/register_site
POST /api/commands/activate_site
POST /api/commands/register_material
POST /api/commands/receive_batch
POST /api/commands/quarantine_batch / release_batch
POST /api/commands/register_subject
POST /api/commands/sign_consent
POST /api/commands/record_eligibility
POST /api/commands/enroll_subject
POST /api/commands/define_cohort
POST /api/commands/submit_cohort
POST /api/commands/review_cohort          # role=安全委员会
POST /api/commands/open_cohort / close_cohort
POST /api/commands/assign_cohort
POST /api/commands/emergency_deviation
POST /api/commands/supplement_deviation
POST /api/commands/review_deviation       # role=安全委员会
POST /api/commands/administer_drug
POST /api/commands/illuminate
POST /api/commands/change_device
POST /api/commands/postpone_procedure
POST /api/commands/finish_treatment
POST /api/commands/report_event
POST /api/commands/notify_committee
POST /api/commands/review_sae             # role=安全委员会, decision=暂停/放行/关闭队列
POST /api/commands/submit_material
POST /api/commands/verify_checksum
POST /api/commands/evaluability
POST /api/commands/record_outcome
POST /api/commands/withdraw_subject
```

查询：

```
GET /api/subjects                受试者列表
GET /api/subjects/<id>?role=盲态评价者   角色视图（自动遮蔽）
GET /api/provenance/<id>         单个受试者完整溯源
GET /api/<资源>                  protocols/cohorts/batches/deviations/events 等
GET /api/audit[?target=<id>]     审计日志
```

规则冲突返回 4xx：`400` 参数、`403` 角色/盲态、`404` 不存在、`409` 状态/闸门/
越窗/重复/版本冲突，`422` 校验值不符；响应体为
`{"error":"<代码>","message":"...","details":{...}}`。

## 六人场景（scenario.py）

S01 标准路径并验证越窗拒收与校验值；S02 照射前器械更换；S03 术期延后、窗口随
实际结束日移动；S04 治疗后 SAE 触发队列暂停、C2 递进冻结、24h 报告、复核恢复；
S05 错误队列/错误照光参数被阻止，激光异常走紧急偏离（先行处置→10.5h 补录→
委员会确认）后完成治疗；S06 治疗后撤回（停新增、保安全随访）。另含六线程并发
入组分配、盲态遮蔽断言与五例结局的完整溯源链断言。
