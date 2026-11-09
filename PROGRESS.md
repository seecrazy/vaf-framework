# VAF 进度文档（PROGRESS）

> 持续维护的交付台账：里程碑、特性状态、测试基线、已知边界。最后更新：2026-11-09。

## 当前状态一览

| 维度 | 状态 |
|---|---|
| 版本 | **V2.1.0**（框架版本常量 `vaf.runtime.FRAMEWORK_VERSION`） |
| 测试 | **161 passed（~1s）**，零外部依赖、离线确定性 |
| CLI | demo / run / inspect / review / clarify / eval / scaffold 全部可用 |
| 示例领域 | medical_triage、ops_incident（均可离线跑通） |
| 环境 | Python 3.12（uv 管理 venv），pydantic v2 / PyYAML / pytest |

## 里程碑时间线

### 2026-11-08 · V2.0.0 —— 设计评审驱动的完整重写
- 输出 V2.0 设计文档：V1.1 评审问题响应矩阵（5 个架构缺口 A1-A5 / 10 处代码硬伤 C1-C10 / 9 项补充机制 B1-B9）；
- L1 契约层：类型化证据链（Finding↔Evidence↔SourceRef 引用闭合）、哨兵差异化校验、强制字段元数据推导、可执行证伪 + 处置服务；
- L2 预处理：节点异常 100% 转异常标记（绝不静默）+ 指标 + 事件，内置 7 组件；
- L3 技能：统一 `build_prompt`、自修复重试（校验错误回喂）、流式滚动校验 + holdback、错误记忆生命周期、grounding 对齐；
- L4 运行时：DAG + Router + 子流程 + 交互澄清暂停 + 人工审核（on_timeout 强制显式）、执行计划快照（版本漂移 fail-fast）、幂等、断点续跑、补偿、AST 白名单表达式引擎；
- 横切：模型路由（数据分级 fail-closed + 熔断）、工具适配（白名单/RBAC/幂等/超时/补偿）、分级脱敏、审计、配置中心（版本/灰度/热更）、可观测三件套、评估闭环（黄金集/门禁/裁判治理/漂移）；
- 示例领域 ×2 + CLI + 测试 140 个。

### 2026-11-09（第一批）· 稳定性收尾
- 修复：父流程续跑时子流程幂等冲突（转 resume 断点重试）+ 回归测试；
- 终检披露边界：流式未接入编排步骤、裁判语义评估为协议桩、多租户/HTTP API/UI 属 Phase 3。

### 2026-11-09（第二批）· V2.1.0 —— 易用性/扩展性专项（本次）
对自评报告的全部建议（P0/P1/P2）逐条落地：

| 编号 | 改进 | 落点 | 状态 |
|---|---|---|---|
| P0-1 | **步骤处理器注册表**：引擎执行分发零 isinstance，新增步骤类型=注册 handler，零引擎改动（测试以自定义 `wait_signal` 步骤验证开闭性） | `runtime/handlers.py`、`engine.register_step_handler` | ✅ |
| P0-2 | **`vaf inspect`**：run 步骤状态/路由/暂停点/副作用/事件/trace 一屏排障（CLI 跨进程验证） | `app.inspect_run`、CLI inspect | ✅ |
| P1-1 | **输出快速构造器**：`quick_output/quick_findings/quick_insufficient` 自动组装引用闭合证据链，契约校验不放松 | `contract/builders.py` | ✅ |
| P1-2 | **提示词模板按技能路由**：`skill.prompt_template_id`；新版本注册即激活；未注册回退核心模板 | `skill/base.py`、`skill/prompt.py` | ✅ |
| P1-3 | **运行期配置读取**：`SkillContext.config`（ConfigView）只读、灰度分桶稳定、版本随快照固化；medical 领域急诊心率阈值示例；配置热更会阻塞旧 run 续跑（A3 联动，有测试） | `runtime/config_view.py`、`medical_triage` | ✅ |
| P2-1 | **领域自动发现**：目录扫描 + entry_points（`vaf.domains` 组，pip 安装即发现）；CLI build_app 已切换 | `runtime/discovery.py` | ✅ |
| P2-2 | **通知中心**：NotificationHub 订阅审核/澄清/证伪/终态事件；webhook_sink（POST JSON + HMAC 签名、重试、绕过系统代理、失败不阻塞主流程；本地 HTTP 服务实测） | `runtime/notification.py` | ✅ |
| P2-3 | **RedisStateStore**：客户端可注入（FakeRedis 全路径测试）、`from_url()` 接 redis-py、可选 TTL | `runtime/persistence.py` | ✅ |
| P2-4 | **GenerationStep 流式步骤**：开闭式扩展首个受益者——流式滚动校验成为普通可编排步骤（违规熔断、降级标记、事件留痕，均有测试） | `runtime/handlers.py::GenerationStepHandler` | ✅ |

配套修订：
- `Pipeline.steps` 开放步骤联合：内置六类 YAML 判别联合，自定义类型 Python 实例构造（类保留）；YAML 未知 type 标签解析期拒绝（防字段静默丢失）；
- 引擎 `validate_pipeline` 的依赖检查对无 `depends_on` 的自定义步骤容错；`handler_for` 对裸 BaseModel 给出引导性报错；
- PromptManager 防御式模板回退（未注册 ID 不崩溃）；
- 测试 140 → **161**（新增 test_v21.py 覆盖上述全部特性）。

## 测试基线

```
161 passed in ~1.1s
tests/
├── 契约/证伪/预处理/表达式/LLM/工具/安全/技能单元 …… test_contract 等 9 个文件
├── 运行时基础（持久化/审核队列/快照/YAML/拓扑）… test_runtime_units
├── 配置中心 + 评估闭环 ……………………………… test_config_eval
├── 引擎校验矩阵 + 快照保护 ………………………… test_engine_validation
├── medical / ops 端到端 …………………………… test_e2e_medical / test_e2e_ops
└── V2.1 新特性 ………………………………………… test_v21
```

## 已知边界（设计内，非缺陷）

1. 裁判语义评估：JudgeGovernance（登记/校准/变更告警）完整，具体 LLM 打分器需实现 `JudgeModel` 协议接入真实模型；结构化指标门禁全功能可用；
2. 多租户强隔离 / HTTP API Server / 审核工作台 UI：Phase 3（当前以 RBAC + tenant 灰度 + webhook 通知覆盖基础面）；
3. 单进程编排（线程池并行）；分布式执行依赖 StateStore 协议扩展（Redis 实现已就绪）；
4. LLM 真实 Provider：实现 `LLMClient` 协议并注册 ModelRouter 即可，框架侧零改动（MockLLM 已验证全部链路）。

## 下一步建议（未排期）

- 真实 LLM Provider 适配器（含流式）与多供应商路由压测；
- 审核工作台最小 Web UI（复用 review queue + webhook）；
- 黄金集管理 CLI（沉淀负样本 → 回归数据集）；
- 领域模板市场（scaffold 模板库 + entry_points 分发）。
