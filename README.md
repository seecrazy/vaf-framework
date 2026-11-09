# VAF 2.1 — 垂直领域 Agent 通用可靠性框架

Vertical Agent Framework：为「结论可枚举、推理步骤可命名、输出可校验」的垂直领域（医疗分诊、投研、信贷审批、运维排障、工单质检……）提供**生产级可靠性骨架**。框架零领域知识；领域开发者只写 4 样东西：**契约（L1）、预处理节点（L2）、推理技能（L3）、Pipeline 编排（L4 配置）**。

> - 完整设计详见 [DESIGN.md](DESIGN.md)（V1.1→V2.0 问题响应矩阵 + V2.1 易用性/扩展性修订）
> - 交付台账与已知边界见 [PROGRESS.md](PROGRESS.md)

## 快速开始

```bash
# 环境：Python ≥3.10
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"

vaf demo        # 离线演示：澄清回路 → 人工审核 → 路由探测 → 子流程 → 评估门禁
pytest          # 161 个用例（~1s），零外部依赖
```

## 五分钟接入一个新领域

```bash
vaf scaffold credit_review          # 生成领域包模板（实测可直接加载运行）
```

填充四个模块（详见示例领域 `domains/medical_triage/`）：

```python
class ApprovalConclusion(str, Enum):
    APPROVE = "approve"; REJECT = "reject"; MANUAL = "manual"
    INSUFFICIENT = INSUFFICIENT_DATA          # 哨兵成员（注册期强制校验）

class ApplicantInput(DomainInput): ...        # L1 输入契约
class ApprovalOutput(SkillOutput):            # L1 输出契约（类型化证据链）
    conclusion: ApprovalConclusion

class CreditPreprocessor(Preprocessor): ...   # L2 确定性预处理（硬规则不给 LLM）

class RiskAssessSkill(AnalysisSkill):         # L3 可独立测试的推理单元
    name, output_contract = "risk_assess", ApprovalOutput
    def build_prompt(self, ctx): ...          # ctx.config 可读运行期配置

PIPELINE = Pipeline(name="credit_review", domain="credit_review", steps=[...])  # L4 YAML/代码

app.register_domain(name="credit_review", input_contract=ApplicantInput,
                    pipeline=PIPELINE, skills=[...], synthesizer=...)
```

**确定性代码里组装输出**（审核修订 / 固定规则技能 / 测试黄金集）用快速构造器，契约校验不放松：

```python
from vaf.contract.builders import quick_output, quick_falsification

output = quick_output(
    ApprovalOutput, conclusion=ApprovalConclusion.REJECT, confidence=Confidence.HIGH,
    claims=[("负债收入比超标", "DTI=87%（征信报告）", SourceGrade.L1, "credit_report")],
    falsifications=[quick_falsification("补充收入证明", "DTI≤70%", "改判人工复核")],
)
```

## 架构总览

```
L4 Runtime   编排引擎（开闭式步骤处理器注册表：skill/human_review/interaction/
             router/sub_pipeline/generation + 自定义类型注册即用）
             快照/幂等/断点续跑/补偿 · 审核队列 · 安全表达式引擎 · 通知中心(webhook)
             配置中心（版本/环境/灰度/热更 + 技能运行期读取）
横切          LLM 抽象（能力协商/自修复重试/模型路由·数据分级硬绑定/熔断）
             工具适配（白名单/RBAC/幂等/超时/审计/补偿） · 安全（分级脱敏/RBAC/审计）
             可观测（指标/链路/事件） · 评估闭环（黄金集/门禁/裁判治理/漂移检测）
             领域自动发现（目录扫描 + entry_points）
L3 Skill     Analysis / Extraction / Generation(流式滚动校验) / Audit（否决+修订）
             统一 build_prompt · 模板按技能路由 · 错误记忆生命周期 · grounding 校验
             输出快速构造器（quick_output 等）
L2 Preprocessor  Filter→Dedup→Enrich→Validate→Score（节点异常 100% 转异常标记，绝不静默）
L1 Contract  类型化证据链（Finding↔Evidence↔SourceRef 引用闭合）· 哨兵差异化校验
             可执行证伪（checker 注册 fail-fast + 处置闭环）
```

## 核心能力速查

| 能力 | 一句话 | 入口 |
|---|---|---|
| 交互澄清回路 | `insufficient_data` → 提问 → 暂停 → 回答 → 重入 | `InteractionStep` / `app.submit_clarification` |
| Router 动态选路 | 运行时按状态选下一跳，未命中分支剪枝 | `RouterStep` |
| 执行计划快照 | run 绑定全版本指纹；热更只影响新 run；续跑漂移 fail-fast | `resume(migrate=…)` |
| 流式生成 | 滚动校验 + holdback 缓冲，违规熔断 | `GenerationStep` 或 `executor.stream_execute` |
| 自修复重试 | Schema 校验错误回喂模型（默认 2 轮） | `SkillExecutor` 内建 |
| 模型路由 fail-closed | confidential 数据无合规端点直接拒绝 | `ModelRouter` |
| 证伪处置闭环 | 触发→影响面→通知→失效→建议重跑 | `FalsificationEngine` + `DispositionService` |
| 断点续跑 | FAILED run 从失败步骤重试，已完成步骤幂等跳过 | `app.resume` |
| 工具补偿 | 非幂等副作用执行前登记 undo，失败逆序补偿 | `ToolSpec` + `register_tool(..., undo_fn=…)` |
| webhook 通知 | 审核/澄清/证伪事件推送（HMAC 签名） | `--webhook URL` / `NotificationHub` |

## CLI

```bash
vaf demo                                              # 离线全流程演示
vaf run --domain medical_triage --input case.json --request-id r1 [--webhook URL]
vaf inspect <run_id> [--json]                         # run 排障一屏看清
vaf review list / approve <task_id> --reviewer alice
vaf clarify submit <run_id> answers.json
vaf eval --domain medical_triage --dataset golden.yaml --gates conclusion_accuracy
vaf scaffold <domain_name>                            # 生成领域包模板
```

`vaf run/inspect` 默认自动发现 `domains/` 目录下的领域包；第三方领域包通过 pip 安装并在
`pyproject.toml` 声明 entry_points 组 `vaf.domains` 即被自动发现。

## 示例领域（可运行）

- **`domains/medical_triage/`** — 澄清回路 + emergency 人工审核（超时 reject）+ 低置信终审降级 + 体温证伪回检 + 运行期配置阈值（ConfigView）+ SQLite 跨实例续跑
- **`domains/ops_incident/`** — 假设置信度路由 + log_probe 工具探测循环 + 子流程报告 + 失败断点续跑 + 工具补偿

## 测试

```bash
pytest   # 161 个用例：单元（契约/表达式攻击面/自修复/流式/熔断/灰度/漂移/构造器/
         # webhook 签名/Redis…）+ 端到端（澄清/审核/超时/路由剪枝/子流程/幂等/
         # 快照漂移/补偿/自定义步骤开闭性/GenerationStep/配置热更联动）
```

## 边界声明

- 保证：输出过契约、证伪可执行、缺口不隐藏、错误可沉淀、副作用可补偿、版本可快照、数据分级与模型路由硬绑定；
- 不保证：领域判断正确率（取决于领域知识与模型能力）；不内置领域知识；
- 支持：可预结构化流程（DAG）+ 受控动态（Router/子流程）+ 多轮信息收集（Interaction）+ 注册式新步骤类型；无声明步骤集的开放式自主 Agent 不在目标内；
- 真实 LLM：实现 `LLMClient` 协议（`call/stream` + 能力声明）注册到 `ModelRouter`，其余链路零改动。

## 目录结构

```
DESIGN.md    PROGRESS.md    README.md    pyproject.toml
src/vaf/
  contract/(base falsification builders)  preprocessor/  skill/  runtime/
  llm/  tools/  security/  observe/  config/  eval/
  app.py  cli.py  demo_llm.py
domains/medical_triage/  domains/ops_incident/
tests/                    # 161 个测试
scripts/manual_e2e*.py    # 开发期手动验证脚本
```
