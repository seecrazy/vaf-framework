# 垂直领域 Agent 通用可靠性框架 VAF 2.7 —— 设计文档

> V2.0 在 V1.1 四层骨架基础上，针对设计评审发现的 5 个架构缺口与 10 处接口/代码硬伤进行系统性修订；**V2.1 易用性/扩展性专项 → V2.2 零代码领域接入 → V2.3 预置领域库（0 分钟即用）**（步骤处理器注册表、流式步骤、快速构造器、模板路由、运行期配置、inspect 排障、领域自动发现、webhook 通知、Redis 存储）。本文档与 `src/vaf/` 实现代码一一对应，所有接口签名以代码为准。

---

## 一、V1.1 → V2.0 修订说明（问题响应矩阵）

| # | V1.1 问题 | 严重级 | V2.0 对策 | 落点 |
|---|---|---|---|---|
| A1 | 多轮交互回路缺失，`insufficient_data` 是死胡同 | P0 | 新增 **InteractionStep**：技能输出澄清问题 → 流程暂停 WAITING_INPUT → 外部提交答案 → 注入状态 → 重入技能，带轮次上限 | §7.6 / `runtime/steps.py` |
| A2 | 固定 DAG 表达不了探索式推理（RCA 场景错配） | P0 | 新增 **RouterStep**（运行时按状态选下一跳）与 **SubPipelineStep**（子流程），动态性约束在已声明步骤集合内 | §7.7 / §7.8 |
| A3 | 断点续跑 × 热更新 = 版本漂移深坑 | P0 | **执行计划快照（ExecutionPlan）**：run 发起时固化 框架/Pipeline/技能/提示词/配置/模型路由 全版本指纹，续跑校验指纹，不匹配即 fail-fast | §7.10 / `runtime/snapshot.py` |
| A4 | 生成类技能流式输出与事后合规校验冲突 | P0 | **滚动校验流式协议**：checkpoint 分段校验 + 尾部 holdback 缓冲，违规即熔断流 | §9 / `skill/executor.py` |
| A5 | 证伪只有检测没有处置 | P0 | **处置服务**：触发 → 影响面查询（哪些 run 引用该结论）→ 通知 → 标记失效 → 建议重跑，处置动作全留痕 | §6.4 / `contract/falsification.py` |
| C1 | Pydantic v1/v2 混用跑不起来 | P1 | 全面 Pydantic v2（`ConfigDict`），Python ≥3.10 语法 | 全部 |
| C2 | 泛型 `Generic[OutputT]` 形同虚设 | P1 | 删除伪泛型；`output_contract` 约束用 `__init_subclass__` 在类定义期校验 | §5.2 |
| C3 | `mandatory_fields()` 与 Field 定义脱节（双重事实来源） | P1 | 强制字段由 `model_fields` 元数据**自动推导**，不再手写清单 | §4.2 |
| C4 | `insufficient_data` 魔法字符串无类型保证 | P1 | 哨兵常量 + 领域注册期校验（结论枚举必须含哨兵值，否则注册失败） | §4.2 |
| C5 | 医疗示例 `red_flags min_length=1` 逼模型造假（自相矛盾） | P1 | 差异化校验规则明确化：哨兵场景豁免 findings/falsification；示例领域 `red_flags` 默认空 | §4.2 / domains |
| C6 | 执行器死代码 + 四类技能方法名不统一 | P1 | 统一 `build_prompt()` 钩子，执行器零 isinstance 分发 | §5.2 |
| C7 | AuditSkill 返回裸 `str` 绕过契约 | P1 | 审核结果类型化为 `AuditResult`，`revision` 是 SkillOutput 子类实例，必须过同一套 Schema 校验 | §5.2 |
| C8 | 预处理静默吞异常（违反自身可观测承诺） | P1 | 节点异常一律转 `Anomaly` 落入条目 + 指标计数 + 事件上报，abort 策略也先记录再抛出 | §5.3 |
| C9 | `DomainInput` 与 `RawItem` 两套模型断层；内部裸 dict 传递 | P1 | `RawItem.from_domain_input()` 桥接；`SkillContext` 全类型化（pydantic 模型，无裸 dict） | §5.1 / §7.3 |
| C10 | `findings: list[str]` 无法支撑自动审计（声明不可验证） | P1 | **类型化证据链**：`Finding{claim, evidence_refs}` ↔ `Evidence{ev_id, content, source_ref}` ↔ `SourceRef{ref_id, grade}`，引用闭合性由校验器保证，为 grounding 校验铺路 | §4.2 / §5.6 |
| B1 | 结构化输出依赖供应商、失败无修复 | P1 | LLMClient 能力协商 + **自修复重试**（把 pydantic 校验错误回喂模型） | §8.2 |
| B2 | findings 真伪无人校验（幻觉） | P2 | **GroundingChecker**：finding 的 evidence 引用必须能在输入/工具结果池中对齐 | §5.6 |
| B3 | 数据分级与模型路由无绑定（金融医疗数据出境） | P2 | **ModelRouter**：`data_classification → 允许的模型端点` 硬绑定，confidential 级路由到不合规端点直接拒绝 | §8.3 |
| B4 | 裁判模型无治理、评估无冷启动方案 | P2 | JudgeGovernance：judge 版本登记、人工校准样本、judge 变更告警；黄金种子集构建流程 | §13 |
| B5 | 自研表达式沙箱（重造轮子+安全风险） | P2 | `ExpressionEngine` 协议 + 默认 AST 白名单实现（~120 行，可审计），文档声明可替换 CEL | §7.2 |
| B6 | HITL 只有触发没有工作台语义；高危默认值危险 | P2 | ReviewTask 生命周期（pending/approved/rejected/timeout）+ **on_timeout 无默认值，强制显式配置** | §7.9 |
| B7 | 性能目标口径矛盾（2s 技能 vs 5s P95） | P2 | 同步/异步 SLA 分离定义；指标按步骤类型分桶 | §12 |
| B8 | 错误记忆自动规则可能污染 prompt | P2 | 规则生命周期（candidate→active→retired）、置信度、上限淘汰、业务类错误需人工确认才激活 | §11 |
| B9 | Phase 1 含断点续跑过于乐观 | P2 | 路线图修订：Phase 1 幂等+整链重跑，断点续跑与快照一起放 Phase 2（本实现已一次性完成，作为参考实现） | §20 |

---

## 二、设计目标与原则

**不变量（框架强制保证）：**
1. 所有技能输出通过 Schema 校验，不合格即拦截（重试/修复/降级），绝不带病放行；
2. 非哨兵结论必须携带 ≥1 条可执行证伪条件、≥1 条类型化 finding，证据引用闭合；
3. 数据缺口显式声明，预处理节点失败必须转化为可见异常标记，禁止静默吞噬；
4. 每次 run 绑定执行计划快照，热更新只影响新 run，历史 run 语义稳定；
5. 有副作用的工具调用必经适配器：权限校验、幂等封装、超时、审计、补偿登记。

**边界铁律（不变）：** 框架零领域知识；领域开发者只写 L1 契约、L2 节点、L3 技能、Pipeline YAML。

---

## 三、总体架构

```
┌────────────────────────────────────────────────────────────────┐
│ L4 Runtime（框架内核）                                          │
│  编排引擎(DAG/Router/子流程/交互暂停) 状态与快照 幂等 补偿       │
│  审核队列 表达式引擎 配置中心(版本/灰度/热更)                    │
├────────────────────────────────────────────────────────────────┤
│ 横切：LLM抽象(能力协商/自修复/模型路由分级绑定) 工具适配(权限/   │
│  幂等/补偿) 安全(脱敏/RBAC/审计) 可观测(指标/链路/事件) 评估闭环 │
├────────────────────────────────────────────────────────────────┤
│ L3 Skill：Analysis / Extraction / Generation(流式) / Audit      │
│  统一 build_prompt 钩子 · 提示词版本化 · 错误记忆 · grounding    │
├────────────────────────────────────────────────────────────────┤
│ L2 Preprocessor：Filter→Dedup→Enrich→Validate→Score            │
│  类型化节点 · 异常全捕获 · 内置组件库                            │
├────────────────────────────────────────────────────────────────┤
│ L1 Contract：DomainInput / SkillOutput(类型化证据链) /          │
│  证伪条件+校验器+处置 / 哨兵机制 / 数据分级                     │
└────────────────────────────────────────────────────────────────┘
```

**代码目录（与实现一致）：**

```
vaf/
├── pyproject.toml
├── DESIGN.md  README.md
├── src/vaf/
│   ├── contract/        # L1：base.py, falsification.py
│   ├── preprocessor/    # L2：nodes.py, pipeline.py, builtin.py
│   ├── skill/           # L3：base.py, context.py, prompt.py, executor.py, memory.py, grounding.py
│   ├── llm/             # client.py, mock.py, router.py
│   ├── tools/           # registry.py, adapter.py
│   ├── security/        # redaction.py, permissions.py, audit.py
│   ├── observe/         # metrics.py, trace.py, events.py
│   ├── runtime/         # expression.py, state.py, persistence.py, steps.py,
│   │                    # snapshot.py, engine.py, review.py
│   ├── config/          # center.py
│   ├── eval/            # dataset.py, runner.py, judge.py, drift.py
│   ├── app.py           # VafApp 门面
│   └── cli.py           # vaf 命令行
├── domains/
│   ├── medical_triage/  # 示例：交互回路 + 人工审核 + 证伪回检
│   └── ops_incident/    # 示例：Router 动态 RCA + 工具探测 + 子流程
└── tests/               # 单元 + 端到端
```

---

## 四、L1 契约层

### 4.1 基础枚举与结构体（`contract/base.py`）

- `Confidence`: high / medium / low
- `SourceGrade`: L1（原始权威）/ L2（二手整理）/ L3（线索）
- `AnomalyLevel`: fatal / warning / info
- `DataGap{item, reason, impact}`：reason ∈ unavailable|error|not_disclosed|out_of_scope
- `Anomaly{source, level, detail, node?}`：全链路统一的异常标记结构

### 4.2 SkillOutput（类型化证据链）

```
SkillOutput (pydantic, extra=forbid)
├── conclusion: str                 # 领域子类收窄为枚举（须含哨兵）
├── confidence: Confidence
├── findings: list[Finding]         # Finding{finding_id, claim, evidence_refs[], confidence?}
├── evidence: list[Evidence]        # Evidence{ev_id, content, source_ref?}
├── sources: list[SourceRef]        # SourceRef{ref_id, grade, label?}
├── assumptions: list[str]
├── falsification: list[FalsificationCondition]
├── data_gaps: list[DataGap]
├── clarifying_questions: list[str] # 仅哨兵场景允许非空（交互回路用）
├── data_classification / compliance_tags
```

**校验器规则（`model_validator`）：**
1. 引用闭合：`evidence_refs` 必须解析到本输出 `evidence[].ev_id`；`source_ref` 必须解析到 `sources[].ref_id`；
2. **差异化强制**（回应 C4/C5）：`conclusion == 哨兵` 时允许 findings/falsification 为空、要求 clarifying_questions ≥1；否则 findings ≥1、falsification ≥1、clarifying_questions 必须为空；
3. `mandatory_fields()` 由 `model_fields` 元数据自动推导（required 且无默认值的字段 + min_length≥1 的集合字段），消除双重事实来源；
4. 哨兵：`INSUFFICIENT_DATA = "insufficient_data"` 常量 + `is_insufficient()` 判定；领域注册时框架校验结论枚举包含该值（`ensure_insufficient_member()`），缺失即注册失败。

**DomainInput / MultimodalInput**：`extra=forbid`，`raw_id/source/timestamp` 必填；多模态扩展 text/image/document/audio。

### 4.4 可执行证伪（`contract/falsification.py`）

- `FalsificationCondition{signal, threshold, consequence, check_frequency, checker_id?}`
- `FalsificationChecker` ABC：`checker_id` + `check(context) -> bool`
- `FalsificationEngine`：
  - `register()` 登记；**`validate_references(conditions)` 在 Pipeline 加载期校验 checker_id 已注册，未注册 fail-fast**（修复 V1.1 静默跳过缺陷）；
  - `run_realtime_checks()`：推理后即时校验，命中标记结论失效；
  - `run_scheduled_checks()`：批量回检历史结论。
- **处置服务（A5）**：`FalsificationEvent{condition, run_id, step, occurred_at}` → `DispositionService`：
  1. `impact`：按结论指纹查引用了该结论的 run（含子流程引用）；
  2. `notify`：写入事件流 + 通知 sink；
  3. `invalidate`：将目标 run 标记 `INVALIDATED`（不删除，保审计）；
  4. `recommend_rerun`：生成重跑建议（新 request_id，携带旧 run 引用）。
  全流程写审计日志。

### 4.5 输出快速构造器（V2.1 P1-1，`contract/builders.py`）

面向确定性技能 / 审核修订 / 测试与黄金集，消除证据链手写仪式感：

- `quick_findings([(claim, evidence[, grade, label]), ...])` → 自动编号且引用闭合的
  findings/evidence/sources（同 label 来源去重共享 ref_id）；
- `quick_output(output_cls, conclusion=..., confidence=..., claims=[...], falsifications=[...])`
  → 完整 SkillOutput（证伪缺省给一条占位）；
- `quick_insufficient(output_cls, questions=[...])` → 哨兵输出（data_gaps 自动声明）。

**不放松任何契约校验**：产物走同一套模型校验器，可靠性不变，只是编号与组装交给框架。

---

## 五、L2 预处理层

### 5.1 数据模型（C9）

`RawItem{id, payload, source, timestamp}`（pydantic），`RawItem.from_domain_input(di)` 提供 L1→L2 桥接；`PreprocessedItem{item, score, anomalies, available_fields, enriched_data}` 全类型化。

### 5.2 原子节点

五类节点基类：`FilterNode / DeduplicateNode / EnrichNode / ValidatorNode / ScorerNode`，每个节点声明 `name` 与失败策略 `fail_strategy ∈ abort|skip|fallback`（fallback 需提供 `fallback_value`）。

### 5.3 失败处理（C8）

节点抛出的任何异常：
1. 转为 `Anomaly{source=node.name, level=策略映射, detail=异常信息}` 附加到**受影响条目**（批量节点附加到全部条目）；
2. 计数指标 `preprocessor_node_failures{node}` +1；
3. 事件上报（结构化 JSONL）；
4. 此后才按策略分流：abort → 记录后 re-raise；skip → 继续无该节点结果；fallback → 注入 fallback_value。
**任何路径都不会静默。**

### 5.4 内置组件库（`preprocessor/builtin.py`）

`HashDeduplicateNode`（payload 指纹去重）、`KeywordFilterNode`、`FieldPresenceValidator`（缺失字段→anomaly）、`RangeValidatorNode`（数值区间）、`PIIDetectionNode`（正则检出 PII→anomaly，配合脱敏）、`LengthTruncateNode`、`StaticScorerNode`（字段映射打分）、**`DocumentExtractNode`（V2.7：http(s)/本地/data: URL 文档文本抽取，html 剥标签；PDF/DOCX 注入自定义 extractor）**。

### 5.5 执行顺序

固定：Filter → Dedup → Enrich → Validate → Score → 组装（按分数降序）。FATAL 异常条目默认仍保留在结果中（由下游 condition 决定拦截或放行），拦截决策显式化属于领域策略。

---

## 六、L3 技能层

### 6.1 技能分类（C2/C6/C7）

统一钩子：所有技能实现 `build_prompt(ctx) -> str`；输出契约统一 `output_contract: type[BaseModel]`。

- `AnalysisSkill`：output_contract 必须是 SkillOutput 子类（`__init_subclass__` 期校验）；
- `ExtractionSkill`：output_contract 为任意结构化实体模型（无需证伪三件套）；
- `GenerationSkill`：自然语言输出，实现 `validate_chunk()`（滚动）与 `validate_generation()`（终检）；
- `AuditSkill`：`audit(target, ctx) -> AuditResult{passed, issues, revision?}`，`revision` 是 SkillOutput 实例（过同一套校验，修复 C7）。

技能可声明：`tools()`（可调用工具白名单）、`knowledge_bases()`、`few_shot_sets()`、`degrade_strategy()`、`model_tier()`、`stream_enabled`。

### 6.2 执行器（`skill/executor.py`）

1. PromptManager 组装：系统模板（版本化）+ **输出契约 JSON Schema 注入** + 通用规则（证伪强制/缺口声明/JSON-only）+ 数据疑点 + 错误记忆 + 工具清单 + 领域提示词；
2. 工具循环（上限 `max_tool_rounds`）：tool_calls 经 ToolAdapter 执行（权限/幂等/超时/审计）；
3. **自修复重试（B1）**：JSON 解析或 Schema 校验失败 → 将模型原始输出 + 校验错误明细作为新消息回喂，最多 `max_repair_attempts`（默认 2）轮；仍失败按步骤 `on_schema_fail` 策略（retry/degrade/skip/abort）处理；
4. 流式执行见 §9；
5. 全程 span + token 用量上报。

### 6.3 SkillContext（C9）

全类型化：`domain_input / preprocessed: list[PreprocessedItem] / upstream_outputs: dict[str, BaseModel] / state_view: StateView（字段级 ACL 的只读视图）/ error_rules / available_tools / request_id / loop_count / clarifications`。

### 6.4 提示词管理（`skill/prompt.py`）

`PromptTemplate{id, version, body}` 注册表；`PromptManager.build()` 注入通用规则并按数据分级执行脱敏（见 §10.2）；模板支持注册/查版本/回滚；**模板版本计入执行计划快照**。

**按技能路由（V2.1 P1-2）**：技能声明 `prompt_template_id` 即可使用自定义模板（须支持同套占位符：domain/skill/sentinel/anomalies/error_rules/tools/clarifications/output_schema/domain_prompt）；新版本注册即激活；未注册 ID 防御式回退核心模板。

### 6.5 错误记忆（B8，`skill/memory.py`）

`ErrorRule{id, kind(schema|business), scope(skill|domain), pattern, rule_text, status(candidate|active|retired), confidence, created_at, expires_at?, hits}`：
- Schema 类：自动激活（confidence 初始 0.7），命中递增；
- 业务类：人工确认后激活（candidate→active）；
- 冲突处理：同 scope 同 pattern 后到覆盖；每技能 active 规则上限（默认 20），超限淘汰 confidence×命中 最低者；
- 过期自动退役；注入 prompt 前过滤 status==active。

### 6.6 Grounding 校验（B10/C10，`skill/grounding.py`）

`GroundingChecker.verify(output, evidence_pool) -> GroundingReport`：finding 的每个 evidence 引用须能解析，且 evidence.content（归一化后）须在证据池（输入 payload + 工具返回 + 上游输出文本）中命中。未命中 → grounding_score 下降 + anomaly + 指标。技能声明 `grounding_required=True` 时不达标触发 on_schema_fail 策略。

---

## 七、L4 运行时

### 7.1 Pipeline 模型（`runtime/steps.py`，discriminated union）

| 步骤类型 | 关键字段 | 语义 |
|---|---|---|
| `skill` | skill, depends_on, parallel_group, condition, loop_condition, max_loops, on_schema_fail, max_retries, state_acl | 执行一个技能；条件/循环/失败策略 |
| `human_review` | trigger_condition, timeout_seconds, **on_timeout（无默认，必填）**, review_fields | 高危决策人工审核（B6） |
| `interaction` | skill, trigger_condition, max_rounds | 澄清回路（A1） |
| `router` | routes[{condition, go_to}], default_next | 运行时选下一跳（A2） |
| `sub_pipeline` | pipeline, input_from | 子流程（A2） |
| `generation`（V2.1） | skill, checkpoint_size, holdback_size | 流式生成：滚动校验 + holdback，走 §9 协议 |

Pipeline 加载期校验：依赖无环、引用的技能/校验器/子流程已注册、条件表达式语法预编译、human_review 显式声明 on_timeout、interaction 技能的输出契约含哨兵。

**开放步骤联合（V2.1）**：`steps: List[Union[AnyStep, BaseModel]]` —— 内置六类走判别联合（YAML/Dict 可解析）；自定义步骤类型以 **Python 实例**直接构造 Pipeline（类信息保留），由 §7.12 的处理器执行。YAML 中出现未注册 type 标签在解析期即拒绝（防字段静默丢失）。

### 7.2 表达式引擎（B5，`runtime/expression.py`）

`ExpressionEngine` 协议（可替换为 CEL 实现）。默认实现 `SafeASTExpressionEngine`：
- 基于 Python `ast` 白名单解析：BoolOp / UnaryOp(Not) / Compare（含 in）/ BinOp（+,-,*,/,%）/ Constant(str,int,float,bool,None) / Name / Attribute（仅 dict 与 pydantic 模型的字段访问，禁止 dunder）/ Subscript / Call（仅 len, abs, min, max, round, str, int, float）；
- 上下文根命名空间白名单：`steps / vars / routers / loop_count / preprocessed / input`；
- 编译期语法检查（Pipeline 加载时执行），运行期拒绝一切越权访问；
- 实现约百行、无 eval/exec、可审计。

### 7.3 状态模型（`runtime/state.py`）

`PipelineState`（pydantic）：request_id, plan_hash, status, step_states{name→StepStatus{status, output?, attempts, started/finished}}, variables（含 clarifications）, tool_calls, side_effects（补偿登记）, reviews, falsification_flags, created/updated。

状态机：`PENDING → RUNNING → (WAITING_REVIEW | WAITING_INPUT)* → SUCCEEDED | FAILED | CANCELLED`；证伪处置可追加 `INVALIDATED`。

**字段级 ACL（StateView）**：`StateView(allowed_prefixes)` 只暴露技能 `state_acl` 声明的字段（默认仅 steps.*.output 与 vars.public.*），越权读取抛 `PermissionError`。

### 7.4 持久化（`runtime/persistence.py`）

`StateStore` 协议：save/get/list_by_status/update_status。实现：`InMemoryStateStore`（测试）、`SqliteStateStore`（生产单机/演示，WAL 模式，JSON 序列化）、**`RedisStateStore`（V2.1 P2-3，客户端可注入便于测试；`from_url()` 走真实 redis-py；可选 TTL）**。按 request_id 索引，天然幂等锚点。

### 7.5 幂等（A3 前置）

`IdempotencyManager`：相同 request_id 二次提交 → SUCCEEDED 返回缓存结果；RUNNING/WAITING 返回当前句柄；失败 run 重放按步骤级状态跳过已完成步骤。

### 7.6 InteractionStep（A1）

执行 → 技能输出哨兵结论（或 trigger_condition 命中）→ 引擎提取 `clarifying_questions` 创建 `ClarificationTask` → run 置 WAITING_INPUT 并持久化 → 返回句柄。外部 `app.submit_clarification(run_id, answers)` → 答案写入 `variables.clarifications` → 步骤 loop_count+1 重入（技能在 prompt 中获得答案），直到非哨兵结论或达 max_rounds（达上限按 on_schema_fail 策略处理）。

### 7.7 RouterStep（A2）

路由结果写入 `routers.<name>.selected`；兄弟步骤用 `condition: "routers.r1.selected == 'deep_probe'"` 控制激活。未匹配任何 route 走 default_next；引擎将不可达步骤置 SKIPPED。适用于"假设→验证→再探测"的探索式 RCA。

### 7.8 SubPipelineStep（A2）

引擎递归执行子 Pipeline，子 run_id = `{parent}:{step}`，状态独立持久化，完成后摘要（SUCCEEDED/FAILED + output）映射回父步骤 output；子 plan_hash 计入父快照。

### 7.9 人工审核（B6，`runtime/review.py`）

`ReviewTask{id, run_id, step, review_fields, payload_digest, status, created_at, deadline, decision?, reviewer?, comment?}`；
`ReviewQueue`：list/pending/approve/reject/sweep_timeouts；
on_timeout ∈ pass（放行）/ reject（步骤失败，走失败策略）/ degrade（标记降级继续）——**加载期强制显式配置**；
超时由 `sweep_timeouts(now)` 统一结算，审核决策写审计日志。

### 7.10 执行计划快照（A3，`runtime/snapshot.py`）

`ExecutionPlan{plan_hash, framework_version, pipeline_name+version, skill_versions, prompt_versions, config_versions, model_route_digest}`；run 创建时固化并随状态持久化。**续跑时校验当前 plan_hash 与状态内一致，不一致抛 `PlanMismatchError`**（显式 `migrate=True` 才允许带迁移标记继续）。配置热更新只影响新 run。

### 7.11 编排引擎（`runtime/engine.py`）

- 编译期：拓扑排序 + 环检测 + 全量加载期校验（§7.1）；
- 执行期：ready 集合（依赖完成且 condition 通过）调度；`parallel_group` 用线程池并行；condition/loop_condition 经表达式引擎求值；
- 步骤失败：按 on_schema_fail/max_retries 处理，终态失败 → 触发补偿（side_effects 逆序 undo）→ run FAILED；
- 暂停点：WAITING_REVIEW / WAITING_INPUT 状态持久化后返回 `RunHandle`；`app.resume(run_id)` 从断点继续（幂等跳过已完成步骤）；
- 全链路 trace：run → step → skill(llm/tool) span 层级。

### 7.12 步骤处理器注册表（V2.1 P0-1，`runtime/handlers.py`）

引擎对"新增步骤类型"**开闭**：执行分发与类型校验全部委托 `StepHandler`：

```
StepHandler
├── step_type: Type[BaseModel]           # 声明管辖的步骤模型
├── validate(step, pipeline, reg, engine) # 加载期校验钩子
└── run(ctx: StepRunContext) -> bool      # 执行体；返回 True = 流程暂停

engine.register_step_handler(MyHandler())  # 新步骤类型 = 注册一个处理器，零引擎改动
```

内置六类处理器（SkillStep / HumanReview / Interaction / Router / SubPipeline / **Generation**）；
引擎的执行分发零 isinstance（仅保留表达式收集处一处类型判断）。GenerationStep 为该机制的
首个受益者：流式生成成为普通可编排步骤（§9）。

### 7.13 通知中心（V2.1 P2-2，`runtime/notification.py`）

`NotificationHub` 订阅 EventBus，把 review_requested / review_timeout /
clarification_requested / falsification_triggered / run_finished 推给 sinks；
`webhook_sink(url, secret=...)`：POST JSON + HMAC-SHA256 签名头、超时与重试、
显式绕过系统代理（内网直连）、失败只记指标绝不阻塞主流程。

---

## 八、LLM 抽象与模型路由

### 8.1 能力协商（`llm/client.py`）

`LLMClient` 协议：`name / capabilities ⊆ {json, tools, stream} / call(messages, tools?, response_schema?, **opts) -> LLMResponse{content, tool_calls, usage} / stream(messages, **opts)`。执行器按能力降级：无 json 能力 → 在 prompt 中注入 Schema + 解析容错（剥离 markdown 围栏）；无 tools 能力 → 工具循环禁用。

**内置 OpenAI 兼容客户端（V2.6，`llm/openai_compat.py`）**：接真实 LLM 零代码——`VAF_LLM_BASE_URL / VAF_LLM_API_KEY / VAF_LLM_MODEL` 三环境变量（或 CLI `--llm-*` 参数）即装配 `llm-official` 端点；json/tools/stream 能力、SSE 流式、429/5xx 重试、直连绕系统代理、纯标准库。未配置时保持离线 Mock（demo/测试路径不变）；非兼容供应商仍可手写 LLMClient 注册。

### 8.2 自修复重试（B1）

见 §6.2 第 3 条。修复消息模板：assistant 原始输出 + user「输出违反 Schema：{校验错误列表}。仅输出修正后的 JSON，不要解释」。

### 8.3 模型路由器（B3，`llm/router.py`）

`ModelRouter.register(endpoint_name, client, tier, allowed_classifications)`；
`route(skill, data_classification, tenant)`：
1. 过滤 allowed_classifications 不含当前分级的端点——**confidential 数据路由到未授权端点直接 `PolicyViolationError`（fail-closed）**；
2. 按 skill.model_tier 偏好选择，同层多端点按熔断状态与错误率选择；
3. 熔断器：滑动窗口错误率超阈值 → open（冷却期）→ half-open 探测，期间自动切换 fallback tier。
路由 digest 计入快照。

### 8.4 测试替身（`llm/mock.py`）

`ScriptedMockLLM`：脚本化响应队列，可注入解析失败/Schema 违规/超时/工具调用序列，支持流式分块；所有框架测试与示例领域离线可跑。

---

## 九、流式生成与滚动校验（A4）

`GenerationSkill` 输出通过 `executor.stream_execute()`：
1. LLM 流式 token → 追加缓冲；
2. 每积累 `checkpoint_size` 字符触发 `validate_chunk(buffer)`；
3. **holdback 尾部缓冲**：只向消费者释放「已通过校验的边界再回退 holdback_size 字符」之前的内容，防止半句违规内容先泄出；
4. 违规 → `StreamViolationError` → 策略 abort_stream（默认，熔断流）/ truncate_and_flag（截断+标记降级）；
5. 流结束 → `validate_generation()` 全文终检，结论写入状态（含降级标记）。

---

## 十、安全体系

### 10.1 RBAC（`security/permissions.py`）

User→Role→Permission；Permission 覆盖 run/submit/config_write/review_approve/audit_read/export。`PermissionChecker.require(user, perm)` 用于 API 与 CLI 动作。审核动作可要求角色（如 medical_reviewer）。

### 10.2 分级脱敏（`security/redaction.py`）

`RedactionPolicy`：分级 → 规则集。sensitive/confidential：PII 正则（手机号/身份证/邮箱/卡号）在进入 prompt、日志、事件前打码；internal：原文+留痕；public：原文。`Redactor.apply(text, classification)`；技能输出声明分级，PromptManager 与事件上报统一走该出口。

### 10.3 审计（`security/audit.py`）

`AuditSink` 协议 + `JsonlAuditSink` / `MemoryAuditSink`；记录 actor/action/target/detail/request_id/时间戳；append-only；覆盖：run 生命周期、审核决策、澄清提交、配置变更、工具调用、导出、证伪处置。

---

## 十一、错误记忆（B8）

见 §6.5。要点：自动激活仅限 Schema 类；业务类必须人工确认；规则有生命周期与容量上限；注入前过滤。

---

## 十二、可观测性（B7）

- 指标（`observe/metrics.py`）：Counter/Gauge/Histogram（含 p50/p95/p99）；关键序列：run_total/run_result、step_duration、llm_tokens/llm_repair_success、tool_calls/failures、review_timeouts、preprocessor_node_failures、grounding_score。**SLA 分桶**：sync 接口（P95≤5s）与 async run（P95 按领域配置）分开统计，step_duration 按 skill 类型分桶——消除 V1.1 口径矛盾；
- 链路（`observe/trace.py`）：span 树（run/step/skill/llm/tool），导出 JSON；
- 事件（`observe/events.py`）：结构化 JSONL 事件流（含脱敏），可对接外部 sink。

---

## 十三、评估闭环

- 黄金集（`eval/dataset.py`）：GoldenCase{case_id, input, expected_conclusion?, expected_findings?, tags}，YAML/代码加载；
- Runner（`eval/runner.py`）：批量执行 → 结构化指标（结论准确率/必填合规率/证伪提供率/grounding 达标率）+ 门禁 `Gate{metric, op, threshold}` 低于阈值阻断发布（返回 gate_passed=False 与明细）；
- 裁判治理（`eval/judge.py`）：`JudgeGovernance.register(model, version, calibration_n, agreement)`——语义评估前必须登记且 agreement ≥ 阈值；judge 模型/版本与历史不一致时告警降权；
- 漂移检测（`eval/drift.py`）：JS 散度比较输入字段缺失率/结论分布/置信度分布突变 + 指标 3σ 控制限 → `DriftReport{alerts}`。

---

## 十四、配置中心（`config/center.py`）

记录 = key → 版本链（value + checksum + actor + ts）；环境 dev/test/staging/prod 隔离；`get(key, env, tenant?, request_id?)` 解析顺序：灰度规则（tenant_in / percentage by hash(request_id+key)，未命中回落**基线版本**）→ 环境值 → 默认值；`set()` 自动升版本并触发 listeners（热更新）；`rollback(key, env, to_version)`；变更全审计。**热更新仅影响新 run（快照机制）**。

**运行期读取（V2.1 P1-3，`runtime/config_view.py`）**：`SkillContext.config`（ConfigView）供技能在推理期读配置——只读、同 run 灰度分桶稳定、读取 key 的版本随执行计划快照固化（示例：medical_triage 急诊心率阈值 `medical.emergency_hr_threshold`）。

---

## 十五、工具适配层（`tools/`）

`ToolSpec{name, description, params_schema, idempotent, required_permission, timeout_s, has_side_effect}`；
`ToolRegistry.register(fn, spec)`；
`ToolAdapter.call(name, args, ctx)`：
1. 技能白名单校验（skill.tools() 未声明 → 拒绝）；
2. RBAC required_permission 校验；
3. 幂等缓存：has_side_effect 且 idempotent → `request_id+name+args_hash` 命中直接返回；
4. 超时控制（线程 future timeout）；
5. 审计 + 指标；
6. has_side_effect 且非幂等 → 执行前登记补偿（undo 回调）到 state.side_effects。

---

## 十六、示例领域设计

### 16.1 medical_triage（演示：交互 + 审核 + 证伪 + HITL）

- 输入 `PatientInput`（主诉/生命体征/病史/年龄/性别）；
- 预处理：生命体征区间校验（RangeValidator）+ 字段缺失检查 + PII 检出；
- 技能：`symptom_intake`（Extraction）、`vitals_risk_check`（Analysis）、`differential_diagnosis`（Analysis）、`triage_auditor`（Audit）；**`triage_clarify`（Analysis，交互步）**——信息不足时输出哨兵+clarifying_questions；
- Pipeline：intake 并行组 → 澄清（条件：哨兵）→ 诊断 → emergency 人工审核（on_timeout 显式 reject）→ 分层（条件路由）→ 合成；
- 证伪：体温/血压回检 checker（模拟数据变化触发失效→处置）。

### 16.2 ops_incident（演示：Router 动态 RCA + 工具 + 子流程）

- 输入：告警批量（RawItem 列表）；
- 预处理：哈希去重 + severity 打分；
- 技能：`alert_triage`（Analysis）、`hypothesis_gen`（Analysis）、`probe_executor`（Analysis，调 log_probe 工具）、`rca_synthesizer`（Analysis）；
- 工具：`log_probe(target, pattern)`（模拟）；
- Pipeline：triage → 假设生成 → **router**（按假设置信度选 deep_probe / quick_check / 直接合成）→ 探测（loop：证据不足再探测，≤2 轮）→ 子流程 `report_gen` → 合成。

---

## 十七、门面与 CLI

`VafApp`：register_domain（契约/节点/技能/Pipeline 注册+加载期校验）、run（幂等）、resume、submit_review、submit_clarification、list_reviews、falsification 引擎接入、eval 入口、审计/指标导出。

CLI（`python3 -m vaf.cli` / console_script `vaf`）：
`vaf scaffold <name>` 生成领域包模板；`vaf run --domain medical_triage --input case.json [--webhook URL]`；`vaf inspect <run_id> [--json]`（V2.1 P0-2：步骤状态/路由/暂停点/事件一屏排障）；`vaf review list|approve|reject`；`vaf clarify submit <run> <answers.json>`；`vaf eval --domain <d> --dataset <yaml>`；`vaf demo`（离线演示全流程）。

**领域自动发现（V2.1 P2-1，`runtime/discovery.py`）**：`register_discovered(app, dirs)` 扫描目录下含 `register(app)` 的领域包；第三方发行版经 entry_points 组 `vaf.domains` 注册（pip 安装即发现）；CLI 的 `build_app` 已切换到自动发现。

---

## 十八、测试策略

- 单元：契约校验器（哨兵豁免/引用闭合/推导强制字段）、预处理异常捕获矩阵、表达式引擎（含攻击样本：dunder/调用任意函数/import）、自修复重试、流式 holdback、路由 fail-closed、幂等、快照不匹配、审核超时、错误记忆生命周期、脱敏、漂移、快速构造器、模板路由、灰度分桶、Redis 存储、webhook（本地 HTTP 服务 + HMAC 签名）；
- 端到端：medical_triage（澄清→审核→通过）、ops_incident（路由→探测循环→子流程）、断点续跑、证伪触发处置、自定义步骤类型（开闭性验证）、GenerationStep 流式编排、配置热更阻塞旧 run 续跑；
- 全部测试基于 ScriptedMockLLM，零外部依赖、CI 可跑。

---

## 十九、能力边界声明（继承 V1.1 并修订）

框架保证：输出过契约、证伪可执行、缺口不隐藏、错误可沉淀、副作用可补偿、版本可快照、数据分级与模型路由硬绑定。
框架不保证：领域判断正确率；不内置领域知识。
V2.0 明确支持：流程可预结构化（DAG）+ 受控动态（Router/子流程）+ 多轮信息收集（Interaction）；纯开放式探索（无声明步骤集的自主 Agent）不在目标内。

## 二十、路线图（修订版，B9 / V2.1 更新）

- Phase 1：契约/预处理/技能/引擎/幂等+整链重跑/MockLLM/CLI —— ✅ 已实现；
- Phase 2：快照+断点续跑 ✅、SQLite/Redis 存储适配 ✅（V2.1）、真实 LLM Provider 接入（实现 `LLMClient` 协议即可，框架侧零改动）；
- Phase 2.5（V2.1 易用性/扩展性专项）：步骤处理器注册表 ✅、`vaf inspect` ✅、输出快速构造器 ✅、模板按技能路由 ✅、运行期配置读取 ✅、领域自动发现 ✅、webhook 通知 ✅、流式 GenerationStep ✅；
- Phase 3：审核工作台 UI、评估平台化（真实裁判模型接入 `JudgeModel` 协议）、多租户强化；
- Phase 4：事件驱动长流程、多模态深化、领域模板市场。

---

## 二十一、零代码领域接入（V2.2，`vaf/lowcode.py`）

目标：**接一个领域可以只写一个 YAML 文件，不写 Python**。声明面覆盖 V2.1 前需编码的六大件：

| 原需编码 | 现声明方式 |
|---|---|
| 输入契约（pydantic 模型） | `input.fields` 字段表（类型/必填/默认/min/max/pattern/enum）→ 动态模型；`raw_id/source/timestamp` 基类自动要求 |
| 输出契约（含哨兵枚举） | `outputs.<name>.conclusion_enum` → 动态 str-Enum，**insufficient_data 哨兵自动追加**（注册期校验必然通过）；`kind: entity` 支持抽取实体 |
| 技能 build_prompt | `skills[].prompt` 模板 + **{{沙箱表达式}}插值**（命名空间 input/upstream/clarifications/config，与条件表达式共用同一 AST 白名单，配置期预编译校验） |
| 预处理器装配 | `preprocessor` 内置节点声明（field_presence/range/keyword_filter/hash_dedup/pii_detection/length_truncate/static_scorer；验证器支持嵌套点路径 vitals.heart_rate） |
| 终审 audit() | `type: audit` + `veto_when` 表达式 + `downgrade_to`/`confidence_after`（命中即否决并降级，修订走同一套契约校验） |
| 证伪 Checker 类 | `falsification_checkers[].when` 表达式（命名空间 {input, vars}，注册期预编译） |
| 合成器函数 | `result_from: <step>`（缺省取最后一个终端步骤） |

编排沿用既有步骤模型（skill/human_review/interaction/router/sub_pipeline/generation 全部可用）。
入口：`app.load_domain_file("x.domain.yaml")` / `app.load_domain_dict(...)`；
自动发现：`register_discovered` 同时扫描目录下 `*.domain.yaml`；
脚手架：`vaf scaffold <name>` 默认生成可加载的低代码模板（`--style code` 保留完整 Python 控制）。

**边界（仍需代码的逃生舱）**：自定义预处理节点、工具函数（register_tool）、多步结果映射合成器——
按需渐进，不强制二选一（低代码技能与代码技能可在同一 Pipeline 混用）。

示例：`domains/customer_ticket.domain.yaml`（工单分类：抽取+分类+低置信终审降级+
表达式证伪，零 Python）；演示见 `vaf demo` 场景 [3]。

---

## 二十二、预置领域库（V2.3，`vaf/presets.py` + `presets/`）

目标：**0 分钟使用**——常用垂直领域提前写好默认配置，install 即用、try 即体验；
同时作为最好的接入教材（每个预置 = 一个带完整注释的真实领域范例）。

**内容准则（不乱写）**：每个预置的领域知识（风控阈值/法条依据/披露口径）基于真实
领域实践撰写，文件头注释与 index.yaml 的 knowledge 字段声明知识依据与校准要求
（"预置是起点，不是免审通行证"）。

| 预置 | 领域知识锚点 | 变体 |
|---|---|---|
| credit_review 个人信贷预审 | DTI 审慎线 50%、征信人工线 600/拒线 450、用途监管禁止流向 | 零代码 YAML + **代码风格孪生包**（同领域两种接入方式对照教学，孪生演示确定性 DTI 推算节点 + 高金额人工审核） |
| contract_risk_review 合同风险审查 | 民法典 585 条违约金调减（损失 30% 参照）、民诉法协议管辖、496 条格式条款 | 零代码 YAML |
| financial_report_extract 公告抽取 | 定期报告披露格式、来源分级 L1/L2/L3、快报与年报口径差异、缺项必须 data_gaps | 零代码 YAML |
| legal_case_assessment 民商事案件评估 | 民事证明标准"高度盖然性"（最高法证据规定）、民法典 188 条 3 年时效、劳动仲裁前置与 1 年时效（27 条）、保全/公告送达策略；输出强制"不构成法律意见" | 零代码 YAML |
| investment_research_note 投资研究评审 | 扣非净利与经营现金流交叉验证、商誉年度减值测试（CAS 8）、估值方法适用边界（DCF/PE/PB/EV-EBITDA/PS）；合规设计：不做投资建议、输出强制免责标签 | 零代码 YAML |
| document_review 通用文档审查 | 非结构化入口：document_url（http/本地/data:URL）→ 预处理确定性抽取 → {{ enriched.document_text }} 进提示词；纯文本/html 内置，PDF 走注入 extractor | 零代码 YAML |
| sap_fico_journal_check 总账凭证检查 | 借贷合计强制平衡、损益科目缺成本对象→KI235 类报错（FI/CO 集成）、冲销凭证类型 AB；**跨行求和超出声明式预处理能力 → Python 变体作为"何时必须写代码"教学案例** | Python 包 |

机制：
- `vaf preset list/show/install/try`；目录 `presets/index.yaml`（含 knowledge 声明）；
- `install`：复制 YAML 到 domains/ 即被自动发现，接真实 LLM 即用；
- `try`：离线体验——`samples.yaml`（样例输入 + 按技能名路由的脚本化响应，PresetResponder）
  驱动 ScriptedMockLLM，零外部依赖、零副作用；
- 目录解析顺序：--presets-dir → VAF_PRESETS_DIR → 仓库根 presets/。

---

## 二十三、移动接入层（V2.5，`vaf/api/`）

手机**无需安装 App** 即可完成审核/澄清：HTTP 网关 + 消息卡片 + H5 审核台（PWA 可添加到主屏）。

- **网关**（`gateway.py`，FastAPI，OpenAPI 自动生成 /docs）：run 提交/列表/inspect、
  澄清提交、待审列表、审批决策、预置目录、健康检查；
- **鉴权**（`auth.py`）：`X-VAF-API-Key` → RBAC 用户（`key:user:roles` 配置），
  未配置 Key 为开放模式（合成 admin，仅限内网/演示，/health 明示）；
  PermissionDenied→403、VafError→400 全局映射；
- **消息卡片**（`cards.py`）：钉钉 actionCard（timestamp 毫秒 + HMAC-SHA256 加签查询参数）、
  企微 markdown、飞书 interactive（timestamp 秒 + 签名入 body）——按钮直达审核台链接；
  事件 payload 携带 payload_digest/required_role/deadline；
- **H5 审核台**（`static/review.html`）：移动优先单文件 PWA，hash 路由
  （#/ 待办双列、#/task/{id} 审批、#/run/{id}/clarify 澄清问答），Key 存 localStorage；
- **CLI**：`vaf serve --api-key ... --notify-card dingtalk=URL ... --review-base ...`；
- 可选依赖：`pip install "vaf[api]"`（fastapi+uvicorn），核心框架零新增硬依赖。

