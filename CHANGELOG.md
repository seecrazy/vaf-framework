# Changelog

## 2.10.0（2026-11-09）
- P0：通知异步投递（引擎不再被外部 IM 阻塞）、缓存淘汰（typed LRU/InMemory 上限）、网关限流中间件
- P1：VafApp 七大组件依赖注入化、MCP 工具接入（stdio JSON-RPC）、Prometheus /metrics
- P2：ambient_clinical_notes 预置（SOAP+医生签核）、vaf falsify sweep、SSE 事件流、LLMJudge
- P3：CI（Actions+ruff）、CHANGELOG、审计日志链式哈希、SQLite 加密文档纠偏

## 2.9.0（2026-11-09）
- 向量检索（OpenAI 兼容 /embeddings，auto 模式）；legal/investment 规则库接入；黄金集 48 例

## 2.8.0（2026-11-09）
- RetrievalNode（规则库+BM25-lite）；credit 深度编排旗舰；preset eval 黄金集门禁
- 修复：低代码输出字段必填逼造（C5 复现）、interaction 证伪回检缺失

## 2.7.0（2026-11-09）
- DocumentExtractNode（http/本地/data:URL）；CLI 多格式输入（yaml/csv/批量）
- 修复：低代码 spec.preprocessor 未接线（V2.2 起 bug）

## 2.6.0（2026-11-09）
- 内置 OpenAI 兼容客户端；VAF_LLM_* 三环境变量零代码接入真实 LLM

## 2.5.0（2026-11-09）
- 移动接入层：HTTP 网关（FastAPI+OpenAPI）、钉钉/企微/飞书消息卡片、H5 审核台 PWA、vaf serve

## 2.4.0（2026-11-09）
- 预置库扩充：法律诉讼评估 / 投资研究评审 / SAP FICO 凭证检查（Python 变体）

## 2.3.0（2026-11-09）
- 预置领域库：vaf preset list/show/try/install/sample/eval；信贷/合同/公告三预置

## 2.2.0（2026-11-09）
- 零代码领域接入：lowcode.py（字段表→动态契约、{{沙箱表达式}}模板、声明式预处理、result_from）

## 2.1.0（2026-11-09）
- 步骤处理器注册表（引擎开闭式扩展）；vaf inspect；快速构造器；模板路由；ConfigView；
  领域自动发现；NotificationHub；RedisStateStore；GenerationStep

## 2.0.0（2026-11-08）
- V1.1 评审驱动的完整重写：四层架构（契约/预处理/技能/运行时）、类型化证据链、
  哨兵机制、可执行证伪+处置、执行计划快照、断点续跑、自修复重试、评估闭环
