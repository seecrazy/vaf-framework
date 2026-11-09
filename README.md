# VAF — 垂直领域 Agent 通用可靠性框架

![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![License](https://img.shields.io/badge/License-Apache--2.0-green) ![Tests](https://img.shields.io/badge/Tests-all%20passing-brightgreen)

Vertical Agent Framework（VAF）— a production-grade reliability skeleton for vertical-domain agents, where conclusions are enumerable, reasoning steps are nameable, and outputs are verifiable.

**为什么选 VAF？** LangGraph / Pydantic AI 给你的是编排原语；VAF 给你的是整套可靠性底盘——输出契约校验、可执行证伪、数据缺口强制声明、人工审核、执行计划快照、断点续跑、副作用补偿、评估门禁，编排层开箱即用。框架零领域知识：**接入一个领域可以只写一个 YAML 文件，也可以用四层 Python API 完整控制，两者可在同一 Pipeline 混用。**

> - 设计文档：[DESIGN.md](DESIGN.md)（分层详设、问题响应矩阵、机制边界）
> - 交付台账与已知边界：[PROGRESS.md](PROGRESS.md)（里程碑、测试基线、未排期项）
> - License：Apache-2.0

## 快速开始

```bash
git clone <本仓库> && cd vaf-framework
python3 -m venv .venv && source .venv/bin/activate   # 少数环境命令为 python；用 python3 --version 确认 ≥3.10
pip install -e ".[test]"

vaf demo        # 离线全流程演示（零 API Key、零外部服务）：
                # 澄清回路 → 人工审核 → 路由探测 → 零代码领域 → 评估门禁
pytest          # 全量测试，约 1 秒
```

跑通 demo 后，按需走下面三条接入路径之一。

## 接入路径一：预置领域库（0 分钟）

常用领域的默认配置已预置好（内容基于真实领域知识撰写，文件头标注知识依据与校准要求）：

```bash
vaf preset list                     # 信贷预审/合同审查/公告抽取/法律诉讼/投资研究/SAP FICO…
vaf preset try credit_review        # 离线体验（样例输入 + 脚本化响应，零依赖）
vaf preset install credit_review    # 复制到 domains/ 即被自动发现 → 接上真实 LLM 即用
```

预置已含**知识增强**：声明式规则库 + 检索注入（信贷 6 条 / 合同 10 条 / 法律 8 条 / 投研 8 条）；配 `VAF_EMBEDDING_BASE_URL/API_KEY/MODEL` 后检索自动升级为**语义向量检索**（OpenAI 兼容 /embeddings），未配置用关键词；`vaf preset eval <name>` 在配置 `VAF_LLM_*` 后输出真实准确率并过门禁——黄金集 48 例（含 DTI 0.50 恰界、征信 600 恰界、用途违规等边界）。

`credit_review` 同时是**深度编排旗舰**（检索+澄清追问+大额路由人工审核四原语齐用）并附**代码风格孪生实现**（`presets/credit_review/credit_review_python/`）：
同一领域两种写法对照，是理解"声明式何时够用、何时转代码"的最好教材。SAP FICO 预置则演示了
反向决策——跨行借贷求和超出声明式能力，因此整体采用 Python 变体。

## 接入路径二：零代码（一个 YAML 文件）

从预置复制一份改，或按下面的骨架手写（完整能力见 [DESIGN.md §21](DESIGN.md)）：

```yaml
# domains/credit_review.domain.yaml —— 放进 domains/ 即被自动发现
domain: credit_review
input:
  fields:
    - {name: monthly_income, type: float, required: true, min: 0}
    - {name: dti, type: float, required: true}
outputs:
  decision:
    conclusion_enum: [approve, reject, manual_review]   # insufficient_data 哨兵自动追加
    fields: [{name: reasons, type: list}]
skills:
  - name: assess
    type: analysis
    output: decision
    prompt: |
      月收入 {{ input.monthly_income }}，负债收入比 {{ input.dti }}。
      硬规则：DTI>0.7 reject；0.5~0.7 manual_review；否则 approve。
  - name: clarify                      # 信息不足时的澄清技能（配合下方 interaction 步骤）
    type: analysis
    output: decision
    prompt: |
      已有信息：{{ input }}；已获回答：{{ clarifications }}。
      关键信息仍缺失 → 结论 insufficient_data 并追问；否则给出审批结论。
  - name: guard                       # 低置信结论终审否决并降级（零代码）
    type: audit
    output: decision
    veto_when: "output.conclusion != 'manual_review' and output.confidence == 'low'"
    downgrade_to: manual_review
falsification_checkers:
  - {checker_id: income_recheck, when: "input.monthly_income < 1000"}
pipeline:
  name: credit_review
  domain: credit_review
  steps:
    - {name: assess, type: skill, skill: assess}
    - {name: ask, type: interaction, skill: clarify, depends_on: [assess], max_rounds: 2}   # 信息不足→提问→暂停→重入
result_from: assess
```

要点：
- 提示词插值 `{{ input.xxx }}` / `{{ upstream.步骤名 }}` / `{{ config['key'] }}` 与条件表达式共用
  同一 AST 白名单沙箱，**加载期预编译校验**（写错当场报错，不会带到运行时）；
- `insufficient_data` 哨兵自动进入结论枚举：技能输出哨兵 + 追问 → 流程暂停等回答 → 重入，
  `interaction` 步骤两行声明即得交互澄清回路；
- 预处理（区间/PII/去重等 7 种内置节点）、人工审核、路由、子流程、流式生成等步骤类型
  均可在同一 YAML 声明；
- 需要自定义节点或工具函数时再转路径三，不强制二选一——YAML 技能与代码技能可混用。

## 接入路径三：Python（完整控制）

以下最小示例可直接复制运行（配合任一 LLM 端点；此处用测试 Mock 演示）：

```python
import json
from enum import Enum

from vaf.app import VafApp
from vaf.contract.base import INSUFFICIENT_DATA, DomainInput, SkillOutput
from vaf.llm.mock import ScriptedMockLLM            # 生产替换为真实 LLM 端点（见下节）
from vaf.runtime.steps import Pipeline, SkillStep
from vaf.skill.base import AnalysisSkill
from vaf.skill.context import SkillContext


class ApprovalConclusion(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    MANUAL = "manual_review"
    INSUFFICIENT = INSUFFICIENT_DATA                # 哨兵成员（注册期强制校验）


class ApplicantInput(DomainInput):                  # L1 输入契约
    monthly_income: float
    dti: float


class ApprovalOutput(SkillOutput):                  # L1 输出契约（类型化证据链）
    conclusion: ApprovalConclusion


class RiskAssessSkill(AnalysisSkill):               # L3 可独立测试的推理单元
    name = "risk_assess"
    version = "1.0.0"
    output_contract = ApprovalOutput

    def build_prompt(self, ctx: SkillContext) -> str:
        return f"月收入 {ctx.domain_input.monthly_income}，DTI {ctx.domain_input.dti}，给出审批结论"


PIPELINE = Pipeline(                                # L4 编排（代码或 YAML 均可）
    name="credit_demo", domain="credit_demo",
    steps=[SkillStep(name="assess", skill="risk_assess")],
)

app = VafApp()
app.register_model_endpoint(
    "llm",
    ScriptedMockLLM(responder=lambda *a, **k: json.dumps({
        "conclusion": "approve", "confidence": "high",
        "findings": [{"finding_id": "f1", "claim": "DTI 达标", "evidence_refs": ["ev1"]}],
        "evidence": [{"ev_id": "ev1", "content": "dti=0.3"}],
        "falsification": [{"signal": "收入变化", "threshold": "月收入下降 30%", "consequence": "重新评估"}],
    }, ensure_ascii=False)),
    tier=1,
)
app.register_domain(
    name="credit_demo", input_contract=ApplicantInput, pipeline=PIPELINE,
    skills=[RiskAssessSkill()], synthesizer=lambda state, outputs: outputs["assess"],
)
handle = app.run("credit_demo",
                 {"raw_id": "a1", "source": "api", "timestamp": "2026-11-09",
                  "monthly_income": 30000, "dti": 0.3},
                 request_id="r1")
print(handle.status, handle.result["conclusion"])   # RunStatus.SUCCEEDED approve
```

确定性代码里组装输出（审核修订 / 固定规则技能 / 测试黄金集）用快速构造器，契约校验不放松：

```python
from vaf.contract.builders import quick_output, quick_falsification

output = quick_output(
    ApprovalOutput, conclusion=ApprovalConclusion.REJECT, confidence=Confidence.HIGH,
    claims=[("负债收入比超标", "DTI=87%（征信报告）", SourceGrade.L1, "credit_report")],
    falsifications=[quick_falsification("补充收入证明", "DTI≤70%", "改判人工复核")],
)
```

## 输入形态：不止 JSON

- **Python API**：直接传 dict 或模型实例（JSON 只是传输表示）；
- **CLI**：`--input` 支持 `.json`（对象或**数组=批量**）、`.yaml/.yml`、`.csv`（表头字段名，每行一个输入，批量跑）；
- **非结构化文档**：输入给一个 `document_url`（http(s) / 本地路径 / `data:text/plain;base64,...`），
  预处理 `document_extract` 节点确定性抽取文本，技能用 `{{ enriched.document_text }}` 引用——
  见预置 `document_review`（PDF/DOCX/扫描件在 Python 路径注入 extractor，见 `preprocessor/document.py` 示例）。

设计边界：非结构化内容在**入口**被规范化为结构化，进门之后全链路契约校验/幂等/快照/证伪照常生效。

## 生产化：接入真实 LLM（零代码）

OpenAI 的 `/chat/completions` 协议已是事实标准（火山方舟 / OpenAI / DeepSeek / GLM /
Qwen / Kimi 等全部兼容），框架内置了 OpenAI 兼容客户端——**设三个配置即接入，不写任何代码**：

```bash
# 方式一：环境变量（对 CLI / serve / Python 全部入口生效）
export VAF_LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
export VAF_LLM_API_KEY=ark-xxxx
export VAF_LLM_MODEL=doubao-seed-1.6

# 方式二：CLI 参数
vaf serve --llm-base-url ... --llm-api-key ... --llm-model ...
```

生效规则：配置齐备 → 注册 `llm-official` 端点（未配置则保持离线 Mock，demo/测试不受影响）。
内置客户端能力：json/tools/stream 能力协商、429/5xx 自动重试、直连绕过系统代理、仅标准库零依赖。

多个端点/非 OpenAI 兼容供应商（高级路径）：手写 `LLMClient` 子类并注册——
端点支持 tier 分层、数据分级硬绑定（confidential 无合规端点直接拒绝，绝不降级路由）与熔断切换：

```python
app.register_model_endpoint(
    "official", OpenAICompatClient("https://...", "KEY", "model-x"),
    tier=1,
    allowed_classifications={"public", "internal", "sensitive"},  # 数据分级硬绑定
)
# 自定义协议：class MyClient(LLMClient): ...  再 register_model_endpoint 即可
```

## 部署与运维

**手机使用（零 App 安装）**——群卡片点【去处理】→ H5 审核台（可添加到主屏）→ 批准/驳回或回答澄清；原生 App/小程序接同一套 REST API（OpenAPI 文档在 `/docs`）：

```bash
pip install "vaf[api]"
vaf serve --port 8000 \
  --api-key "k_op:ops:operator" --api-key "k_rev:doc:reviewer" \
  --notify-card dingtalk=https://oapi.dingtalk.com/robot/send?access_token=xxx \
  --notify-secret SECxxx
```

**CLI 全命令索引**：

```bash
vaf demo                                     # 离线全流程演示
vaf preset list / show / try / install / sample / eval      # 预置领域库（eval=黄金集准确率，配 LLM 出真实基线）
vaf scaffold <name> [--style code]           # 生成领域模板（默认低代码 YAML）
vaf run --domain <d> --input case.json|form.yaml|rows.csv [--request-id r1] [--webhook URL]   # yaml/csv（表头逐行）/json 数组=批量
vaf inspect <run_id> [--json]                # run 排障一屏看清（步骤/路由/暂停点/事件）
vaf review list / approve <task_id> / reject <task_id> --reviewer <user>
vaf clarify submit <run_id> answers.json
vaf eval --domain <d> --dataset golden.yaml --gates conclusion_accuracy
vaf serve [--api-key ...] [--notify-card ...]
```

`vaf run/inspect/serve` 自动发现 `domains/` 目录（Python 包与 `*.domain.yaml`）；
第三方领域包 pip 安装后在 `pyproject.toml` 声明 entry_points 组 `vaf.domains` 即被发现。

**部署形态**：单进程（默认 SQLite 状态库）→ 多实例（`RedisStateStore.from_url`）→
移动/多端（`vaf serve` 网关 + 消息卡片）。断点续跑与幂等由状态库保证，重启不丢现场。

## 架构一页图

```
L1 Contract      契约与证据链：输入/输出 Schema、insufficient_data 哨兵、
                 Finding↔Evidence↔SourceRef 引用闭合、可执行证伪（fail-fast + 处置闭环）
L2 Preprocessor  确定性预处理：Filter→Dedup→Enrich→Validate→Score
                 （能用规则解决的不给 LLM；节点异常 100% 转标记，绝不静默）
L3 Skill         推理单元：Analysis/Extraction/Generation(流式)/Audit 四类、
                 提示词托管与模板路由、自修复重试、错误记忆、grounding 校验
L4 Runtime       编排运行时：开闭式步骤处理器（skill/review/interaction/router/
                 sub_pipeline/generation + 自定义注册）、状态与快照、幂等与断点续跑、
                 补偿、配置中心（版本/灰度/热更）、通知中心
横切              LLM 路由（分级硬绑定/熔断）· 工具适配（白名单/RBAC/幂等/补偿）
                 安全（分级脱敏/RBAC/审计）· 可观测（指标/链路/事件）· 评估闭环 · 移动接入
```

## 核心能力速查

| 能力 | 一句话 | 入口 |
|---|---|---|
| 交互澄清回路 | `insufficient_data` → 提问 → 暂停 → 回答 → 重入 | YAML `interaction` 步骤 / `app.submit_clarification` |
| Router 动态选路 | 运行时按状态选下一跳，未命中分支剪枝 | YAML `router` 步骤 |
| 人工审核 | 高危决策暂停等人；`on_timeout` 必须显式声明 | YAML `human_review` 步骤 / H5 审核台 |
| 流式生成 | 滚动校验 + holdback 缓冲，违规即熔断 | YAML `generation` 步骤 |
| 执行计划快照 | run 绑定全版本指纹；热更只影响新 run；续跑漂移即拒 | `app.resume(migrate=…)` |
| 断点续跑 | FAILED run 从失败步骤重试，已完成步骤幂等跳过 | `app.resume` |
| 自修复重试 | Schema 校验错误回喂模型（默认 2 轮） | 内建（`on_schema_fail` 可配） |
| 证伪处置闭环 | 触发→影响面→通知→失效→建议重跑 | `FalsificationEngine` + `DispositionService` |
| 工具补偿 | 非幂等副作用执行前登记 undo，失败逆序补偿 | `register_tool(fn, spec, undo_fn=…)` |
| 模型路由 fail-closed | confidential 数据无合规端点直接拒绝 | `register_model_endpoint(allowed_classifications=…)` |
| 评估门禁 | 黄金集跑分 + 结论准确率阈值阻断发布 | `vaf eval` / `EvalRunner` |
| 移动闭环 | 消息卡片 → H5 审批/澄清 → 自动续跑，零 App | `vaf serve` + `--notify-card` |

## 示例领域（均可离线运行）

- `domains/medical_triage/` — 澄清回路 + emergency 人工审核（超时 reject）+ 低置信终审降级 + 体温证伪回检 + 运行期配置阈值 + SQLite 跨实例续跑
- `domains/ops_incident/` — 假设置信度路由 + 工具探测循环 + 子流程报告 + 失败断点续跑 + 工具补偿
- `domains/customer_ticket.domain.yaml` — 零代码领域：契约/抽取/分类/终审降级/表达式证伪全在一个文件
- `presets/` — 预置领域库（信贷含代码孪生、合同、公告、法律诉讼、投资研究、SAP FICO），`vaf preset list` 看全部

## 边界声明

- 保证：输出过契约、证伪可执行、缺口不隐藏、错误可沉淀、副作用可补偿、版本可快照、数据分级与模型路由硬绑定；
- 不保证：领域判断正确率（取决于领域知识与模型能力）；不内置领域知识；预置领域内容须按机构政策校准；
- 支持：可预结构化流程（DAG）+ 受控动态（Router/子流程）+ 多轮信息收集（Interaction）+ 注册式新步骤类型；无声明步骤集的开放式自主 Agent 不在目标内。

## 目录结构

```
DESIGN.md  PROGRESS.md  README.md  pyproject.toml
src/vaf/
  contract/    L1 契约（base / falsification / builders 快速构造器）
  preprocessor/  L2 节点与流水线（nodes / pipeline / builtin）
  skill/       L3 技能（base / executor / prompt / memory / grounding）
  runtime/     L4 运行时（engine / handlers 步骤处理器 / steps / state /
               persistence[SQLite/Redis] / snapshot / review / expression /
               notification / discovery / config_view）
  llm/         LLM 抽象（client / router / mock）
  tools/  security/  observe/  config/  eval/     横切能力
  api/         移动接入（gateway / auth / cards / static H5 审核台）
  lowcode.py   零代码装配    presets.py  预置库
  app.py  cli.py  demo_llm.py
domains/       示例领域（2 个 Python + 1 个零代码 YAML）
presets/       预置领域库（低代码 YAML / Python 变体 + 离线样例）
tests/         全量测试（数量见 PROGRESS.md）
scripts/       开发期手动验证脚本
```

---

测试数量、里程碑与已知边界以 [PROGRESS.md](PROGRESS.md) 为唯一事实源；机制详设见 [DESIGN.md](DESIGN.md)。
