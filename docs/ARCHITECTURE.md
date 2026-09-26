# 架构说明与需求映射

版本：0.1.0｜日期：2026-09-27

## 1. 决策背景

《02_详细开发方案》的主线是"Opik 现有产品 + GEPA 搜索内核 + 薄业务扩展服务"，要求 Docker
Linux 容器承载 Celery Worker 与 Opik 全家桶。目标机器（Windows 11，无 Docker Desktop）无法运行该
组合；文档 02 第 11 节明确："若机器无法满足 Opik 部署需求，优先把后台迁到服务器仍用 Windows 浏览器"。

用户指定本系统在本机 Windows 自用。因此采用**降级实现**：把同一业务闭环实现为自包含本地系统
（FastAPI + SQLite + 原生 JS 单页界面），保留文档定义的业务规则、状态机、错误协议与统计口径；
外部依赖接口保持可替换（供应商走 OpenAI 兼容协议、优化器走 GEPA 式回调接口），待有容器/服务器
环境时按 M0 流程接入真实 Opik/GEPA，不推翻业务层。

## 2. 系统结构

```text
Windows Edge / Chrome（仅 127.0.0.1）
        │
   FastAPI（uvicorn 单进程）
   ├── /                      → web/ 中文单页界面（原生JS，全部输出HTML转义）
   └── /workflow-api/v1/...   → 业务REST（错误体统一 code/message/field_errors/retryable/correlation_id）
         ├── SQLite（data/prompt_lab.db，WAL，单写连接+RLock）
         │     项目契约 / 案例与切分 / 封存制品 / 评价标准 / 标注与盲评 / 评价器 /
         │     提示词版本 / 运行快照与事件 / 调用账本 / 验收报告 / 发布与反馈 / 审计
         ├── providers.py     MockProvider（离线确定性，注入429/401/超时/截断/坏JSON）
         │                    OpenAICompatProvider（智谱/DeepSeek/OpenAI，密钥仅本地存储）
         ├── engine.py        白名单渲染 → 账本预留 → 出站调用 → 结算 → 输出落库
         └── runs.py          快照校验 / 幂等 / 状态机 / 独立验收 / 发布
```

## 3. 与 PRD 的页面映射

| PRD 页面 | 实现位置 | 说明 |
|---|---|---|
| P01 项目列表与创建 | `web` P01 + `domain.ProjectsService` | 四任务模板生成契约草案（is_demo 标记） |
| P02 总览与引导 | `domain.ProjectsService.readiness` | 依赖计算状态阶梯，缺项映射补齐页面 |
| P03 案例与数据集 | `domain.DataService` | 预览/提交两段式、哈希幂等、分组泄漏门禁、封存受限表、不可变版本 |
| P04 评价标准 | `domain.RubricService` | 锚点完整性发布校验、版本化、旧报告只读、触发评价器stale |
| P05/P06 标注 | `domain.AnnotationService` | 盲评公开ID、码点证据校验、unknown/both_unusable、model_pre不入gold |
| P07 评价器校准 | `domain.JudgeService` | 构建/审计来源隔离、混淆矩阵、支持数、低样本限制标记、stale |
| P08 提示词库 | `domain.PromptService` | 版本不可变、哈希、白名单变量、diff |
| P09 编辑与试运行 | `api /prompts/{id}/trial` + `engine` | 冻结段校验前置、统一计量、失败/截断分状态 |
| P10 实验配置 | `runs.RunService.validate/estimate/create` | 快照固定全部版本、latest拒绝、预算校验、幂等键 |
| P11 实验运行 | `runs.RunService._execute`（线程） | 事件流、候选树、协作取消、预算暂停、账本对账 |
| P12 独立验收 | `runs.AcceptanceService` | 一次性解封、配对统计、五类结论、不可变报告 |
| P13 使用与反馈 | `runs.ReleaseService` | active需verified报告、trial不改指针、乐观锁、回滚新事件 |
| P14 设置 | `api /settings/*` | 密钥脱敏、价格表、连接测试、脱敏诊断、审计 |

## 4. 与开发方案的模块映射（D01—D11）

| 开发模块 | 本实现 | 偏差说明 |
|---|---|---|
| D01 项目/契约/引导 | ProjectsService + 模板 | 引导对话未做（V1 可选）；准备度计算已实现 |
| D02 导入/分组/封存 | DataService | 近似分组建议未做字符串相似推荐（手动分组）；导入源文件存 settings 受限区 |
| D03 标准/标注/证据 | Rubric/AnnotationService | 逐片段证据完整；合并标签映射未做（V1 P1） |
| D04 评价器校准 | JudgeService + engine.evaluate_once | 一致率/混淆/支持数/弃权完整；顺序交换审计未做（V1 P1） |
| D05 冻结/公平试运行 | PromptService + engine.render_messages | 冻结段+变量白名单+参数归一化完整 |
| D06 快照/队列/幂等 | runs.py + SQLite（线程替代Celery） | 单机同步线程执行；Outbox 未做（单库无需跨系统同步） |
| D07 优化适配 | engine.propose_fragment/score_prompt | GEPA 式接口的确定性本地实现；真实 GEPA 待 M0 |
| D08 预算账本 | ledger.py | 原子预留/sent_unknown/对账完整；价格表按模型粒度 |
| D09 独立验收统计 | AcceptanceService + stats.py | 精确McNemar/分组bootstrap/缺失界限/零事件上界完整 |
| D10 发布/回滚/反馈 | ReleaseService | 乐观锁/回滚完整性/待核验池完整 |
| D11 Windows交付 | scripts/*.ps1 | start/stop/doctor/backup；restore 即手动复制备份+重启（单机SQLite） |

## 5. 错误协议

`code / message（中文可行动解释）/ field_errors / retryable / correlation_id`。
已实现业务错误码：`DATA_SPLIT_LEAKAGE、FROZEN_COMPONENT_CHANGED、VARIABLE_NOT_ALLOWED、
EVIDENCE_MISMATCH、GOLD_NOT_VERIFIED、SOURCE_OVERLAP、JUDGE_STALE、BUDGET_EXHAUSTED、
BUDGET_PHASE_CONFLICT、PRICE_UNKNOWN、IDEMPOTENCY_CONFLICT、PREVIEW_HASH_MISMATCH、
TEST_ALREADY_CONSUMED、CANDIDATE_NOT_LOCKED、REPORT_NOT_VERIFIED、REVISION_CONFLICT、
PROJECT_ARCHIVED、RUBRIC_INVALID、NO_IMPROVEMENT 路径等`。

## 6. 安全边界

- 仅绑定 127.0.0.1；无公网访问（TC058）
- 密钥只存本地 settings，API 响应只回"已配置"，诊断导出不含密钥与封存原文（TC051/TC060）
- 封存测试原文在冻结后从通用表移入 `sealed_artifacts`，普通列表/选择器不可见（TC010）；
  验收解封为一次性消耗并写审计（TC043）
- 前端全部动态内容经 HTML 转义；模型输出按文本渲染（TC051）
- 受限设计阻止应用工作流误用，不防宿主机管理员直接读磁盘（与文档口径一致）
