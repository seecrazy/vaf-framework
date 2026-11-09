"""VAF CLI（DESIGN §17）：demo / run / review / clarify / eval / scaffold。

所有命令默认使用内置示例领域 + ScriptedMockLLM（离线可跑）；
生产接入时替换 --provider 为真实 LLM Client 注册即可。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .app import VafApp
from .llm.mock import ScriptedMockLLM, tool_call
from .security.permissions import User


def _add_llm_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--llm-base-url", default=None, help="OpenAI 兼容服务基址（或环境变量 VAF_LLM_BASE_URL）")
    parser.add_argument("--llm-api-key", default=None, help="API Key（或 VAF_LLM_API_KEY）")
    parser.add_argument("--llm-model", default=None, help="模型名（或 VAF_LLM_MODEL）")


def _llm_kwargs(args: argparse.Namespace) -> dict:
    return {
        "llm_base_url": getattr(args, "llm_base_url", None),
        "llm_api_key": getattr(args, "llm_api_key", None),
        "llm_model": getattr(args, "llm_model", None),
    }


def build_app(
    sqlite_path: Optional[str] = None,
    webhook_url: Optional[str] = None,
    notification_hub=None,
    llm_base_url: Optional[str] = None,
    llm_api_key: Optional[str] = None,
    llm_model: Optional[str] = None,
) -> VafApp:
    """构建 demo 应用：自动发现 domains/ 目录下的领域 + Mock LLM（离线可跑）。"""
    import os
    import sys as _sys

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for p in (os.path.join(root, "src"), os.path.join(root, "domains")):
        if os.path.isdir(p) and p not in _sys.path:
            _sys.path.insert(0, p)

    from .demo_llm import DemoResponder
    from .llm.mock import ScriptedMockLLM
    from .runtime.notification import webhook_sink

    if webhook_url and notification_hub is None:
        from .runtime.notification import NotificationHub

        notification_hub = NotificationHub(sinks=[webhook_sink(webhook_url)])

    app = VafApp(sqlite_path=sqlite_path, notification_hub=notification_hub)
    # 真实 LLM 零代码接入：CLI 参数 > 环境变量 > 离线 Mock（demo/测试路径不受影响）
    from .llm.openai_compat import OpenAICompatClient, client_from_env

    if llm_base_url and llm_model:
        client = OpenAICompatClient(llm_base_url, llm_model, llm_api_key or "")
    else:
        client = client_from_env()
    if client is not None:
        app.register_model_endpoint(client.name, client, tier=1)
    else:
        app.register_model_endpoint(
            "demo-mock", ScriptedMockLLM(name="demo-mock", responder=DemoResponder()), tier=1
        )

    from .runtime.discovery import register_discovered

    domains_dir = os.path.join(root, "domains")
    registered = register_discovered(app, dirs=[domains_dir] if os.path.isdir(domains_dir) else None)
    if not registered:
        raise SystemExit("未发现任何领域包（domains/ 目录或 vaf.domains entry_points）")
    return app


# ---------------------------------------------------------------------------
def cmd_demo(args: argparse.Namespace) -> int:
    app = build_app()
    print("== VAF 2.0 离线演示 ==\n")

    # 场景 1：medical_triage 澄清 + 审核
    print("[1] medical_triage：信息不足 → 澄清 → 急诊 → 人工审核")
    patient = {
        "raw_id": "demo-p-001",
        "source": "hotline",
        "timestamp": "2026-11-08T10:00:00",
        "chief_complaint": "胸痛 3 小时，联系电话 13800138000",
        "vitals": {"heart_rate": 128, "temperature_c": 36.8, "systolic_bp": 150},
        "age": 58,
        "gender": "male",
    }
    h = app.run("medical_triage", patient, "demo-run-1")
    print(f"    首轮: {h.status.value}")
    if h.pending_clarification:
        print(f"    澄清问题: {h.pending_clarification.questions}")
        h = app.submit_clarification(
            "demo-run-1", {"疼痛是否放射至左臂/下颌？": "是", "是否伴出汗恶心？": "是"}
        )
        print(f"    已提交澄清: {h.status.value}")
    if h.pending_review_task:
        print(f"    待审核任务: {h.pending_review_task}")
        h = app.submit_review(
            h.pending_review_task, "approved", reviewer=User("demo-reviewer", {"reviewer"}),
            comment="同意急诊处置",
        )
    print(f"    终态: {h.status.value} 结论: {(h.result or {}).get('conclusion')}\n")

    # 场景 2：ops_incident 路由 + 工具 + 子流程
    print("[2] ops_incident：高置信假设 → 路由深度探测 → 工具验证 → 子流程报告")
    incident = {
        "raw_id": "demo-inc-001",
        "source": "prometheus",
        "timestamp": "2026-11-08T10:01:00",
        "service": "payment",
        "oncall": "alice",
        "alerts": [
            {"id": "a1", "alert_key": "K1", "severity": "critical", "title": "db-proxy conn pool"},
            {"id": "a2", "alert_key": "K1", "severity": "critical", "title": "dup"},
        ],
    }
    h2 = app.run("ops_incident", incident, "demo-run-2")
    r = h2.result or {}
    print(
        f"    终态: {h2.status.value} 根因: {r.get('root_cause')} "
        f"探测轮数: {r.get('probe_rounds')} 动作: {r.get('actions')}\n"
    )

    # 场景 3：零代码领域（customer_ticket.domain.yaml，无任何 Python）
    print("[3] 零代码领域：customer_ticket.domain.yaml（契约/技能/终审/证伪全在 YAML）")
    ticket = {
        "raw_id": "tk-001",
        "source": "app",
        "timestamp": "2026-11-09T10:00:00",
        "subject": "云主机被扣款两次，要求核查",
        "body": "本月账单多扣了 320 元，订单号 8899，麻烦尽快处理。",
        "customer_level": "vip",
        "order_amount": 320.0,
    }
    h3 = app.run("customer_ticket", ticket, "demo-run-3")
    t = h3.result or {}
    print(
        f"    终态: {h3.status.value} 分类: {t.get('conclusion')} "
        f"子类: {t.get('subcategory')} 证伪触发: {h3.falsification_flags or '无'}\n"
    )

    # 场景 4：评估 + 门禁
    print("[4] 评估闭环：黄金集跑分 + 门禁")
    from .eval.dataset import dataset_from_dict
    from .eval.runner import EvalRunner, Gate

    dataset = dataset_from_dict(
        {
            "domain": "medical_triage",
            "cases": [
                {
                    "case_id": "case-emergency",
                    "input": patient,
                    "expected_conclusion": "emergency",
                },
                {
                    "case_id": "case-routine",
                    "input": {
                        **patient,
                        "raw_id": "demo-p-002",
                        "chief_complaint": "轻微咳嗽 2 天",
                        "vitals": {"heart_rate": 76, "temperature_c": 36.5, "systolic_bp": 120},
                    },
                    "expected_conclusion": "routine",
                },
            ],
        }
    )
    report = EvalRunner(app.engine).run(
        dataset, request_prefix="demo-eval", gates=[Gate(metric="conclusion_accuracy", threshold=0.5)]
    )
    print(f"    指标: {report.metrics} 门禁: {'通过' if report.gate_passed else report.failed_gates}")

    print("\n== 演示结束（全部离线、确定性） ==")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    app = build_app(
        sqlite_path=args.state or None,
        webhook_url=getattr(args, "webhook", None),
        **_llm_kwargs(args),
    )
    with open(args.input, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    request_id = args.request_id or payload.get("raw_id", "") or "cli-run"
    handle = app.run(args.domain, payload, request_id)
    print(json.dumps(handle.to_dict(), ensure_ascii=False, indent=2, default=str))
    if handle.pending_clarification:
        print(
            "run 等待澄清，提交：vaf clarify submit "
            f"{request_id} answers.json"
        )
    if handle.pending_review_task:
        print(f"run 等待审核，任务：{handle.pending_review_task}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    app = build_app(sqlite_path=args.state or None)
    if args.action == "list":
        tasks = app.list_pending_reviews()
        print(json.dumps(tasks, ensure_ascii=False, indent=2, default=str))
        return 0
    if not args.task_id:
        print("缺少审核任务 ID，先执行 vaf review list", file=sys.stderr)
        return 2
    reviewer = User(args.reviewer or "cli-user", {"reviewer"} if args.reviewer else set())
    handle = app.submit_review(
        args.task_id,
        args.action,
        reviewer=reviewer if args.action in ("approve", "approved") else None,
        comment=args.comment or "",
    )
    print(json.dumps(handle.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_clarify(args: argparse.Namespace) -> int:
    app = build_app(sqlite_path=args.state or None)
    with open(args.answers, "r", encoding="utf-8") as fh:
        answers = json.load(fh)
    handle = app.submit_clarification(args.run_id, answers)
    print(json.dumps(handle.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    app = build_app(sqlite_path=args.state or None, **_llm_kwargs(args))
    from .eval.dataset import dataset_from_yaml
    from .eval.runner import EvalRunner, Gate

    dataset = dataset_from_yaml(args.dataset)
    gates = [Gate(metric=g) for g in (args.gates or [])]
    report = EvalRunner(app.engine).run(dataset, request_prefix="cli-eval", gates=gates)
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, default=str))
    return 0 if report.gate_passed else 1


def cmd_inspect(args: argparse.Namespace) -> int:
    app = build_app(sqlite_path=args.state or None)
    report = app.inspect_run(args.request_id)
    if report is None:
        print(f"未找到 run: {args.request_id}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    # 人类可读摘要
    print(f"run      : {report['request_id']}")
    print(f"pipeline : {report['pipeline']} ({report['domain']})  status: {report['status']}")
    print(f"plan     : {report['plan_hash']}")
    if report["error"]:
        print(f"error    : {report['error'][:200]}")
    if report["pending_review_task"]:
        print(f"待审核   : {report['pending_review_task']}")
    if report["pending_clarification"]:
        print(f"待澄清   : 第{report['pending_clarification'].get('round_no')}轮 "
              f"{report['pending_clarification'].get('questions')}")
    if report["routers"]:
        print(f"routers  : {report['routers']}")
    if report["falsification_flags"]:
        print(f"证伪触发 : {report['falsification_flags']}")
    print("steps    :")
    for s in report["steps"]:
        line = f"  - {s['step']:<24} {s['status']:<10} attempts={s['attempts']} loops={s['loops']}"
        if s["degraded"]:
            line += f"  ({s['degraded'][:60]})"
        if s["error"]:
            line += f"  err={s['error'][:60]}"
        print(line)
    return 0


def cmd_scaffold(args: argparse.Namespace) -> int:
    import os

    name = args.name
    if args.style == "lowcode":
        base = args.dir or "domains"
        os.makedirs(base, exist_ok=True)
        files = {
            os.path.join(base, f"{name}.domain.yaml"): _SCAFFOLD_LOWCODE % {"name": name},
            os.path.join(base, f"{name}.test_cases.yaml"): "domain: %s\ncases: []\n" % name,
        }
    else:
        base = os.path.join(args.dir or "domains", name)
        os.makedirs(base, exist_ok=True)
        files = {
            os.path.join(base, "__init__.py"): _SCAFFOLD_INIT % {"name": name},
            os.path.join(base, "pipeline.yaml"): _SCAFFOLD_YAML % {"name": name},
            os.path.join(base, "test_cases.yaml"): "domain: %s\ncases: []\n" % name,
        }
    for path, content in files.items():
        if os.path.exists(path) and not args.force:
            print(f"跳过已存在: {path}")
            continue
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"生成: {path}")
    print(
        "提示：低代码领域用 app.load_domain_file / vaf run 直接运行；"
        "需要自定义节点或工具时改用 --style code"
    )
    return 0


_SCAFFOLD_LOWCODE = """# %(name)s 领域（vaf scaffold --style lowcode 生成）
# 本文件即完整领域定义；提示词插值 {{ input.xxx }} / {{ upstream.步骤名 }} 走沙箱表达式。
domain: %(name)s
version: "0.1.0"
data_classification: internal

input:
  fields:
    - {name: payload, type: str, required: true, description: TODO 输入主字段}

outputs:
  main:
    conclusion_enum: [ok, needs_review]   # insufficient_data 哨兵自动追加
    fields:
      - {name: note, type: str}

preprocessor:
  validators:
    - {type: field_presence, fields: [payload]}

skills:
  - name: main_skill
    type: analysis
    output: main
    prompt: |
      处理以下输入并给出结论（ok / needs_review / insufficient_data）：
      {{ input.payload }}
      TODO：在此写领域分类/判断规则。

falsification_checkers: []
#  - {checker_id: %(name)s_recheck, when: "'已完成' in input.payload"}

pipeline:
  name: %(name)s
  domain: %(name)s
  version: "0.1.0"
  steps:
    - name: main_step
      type: skill
      skill: main_skill

result_from: main_step
"""


_SCAFFOLD_INIT = '''"""%(name)s 领域包（vaf scaffold 生成）。"""
from enum import Enum
from typing import List

from pydantic import BaseModel, Field

from vaf.contract.base import (
    INSUFFICIENT_DATA,
    DomainInput,
    SkillOutput,
)
from vaf.preprocessor.pipeline import Preprocessor
from vaf.runtime.steps import InteractionStep, Pipeline, SkillStep
from vaf.skill.base import AnalysisSkill, BaseSkill
from vaf.skill.context import SkillContext


class Conclusion(str, Enum):
    # TODO: 定义领域结论枚举（必须保留哨兵成员）
    OK = "ok"
    INSUFFICIENT = INSUFFICIENT_DATA


class MyInput(DomainInput):
    # TODO: 定义领域输入字段
    payload: str = ""


class MyOutput(SkillOutput):
    conclusion: Conclusion


class MyPreprocessor(Preprocessor):
    def __init__(self) -> None:
        super().__init__()
        # TODO: 装配 filters/validators/scorer


class MySkill(AnalysisSkill):
    name = "my_skill"
    description = "TODO"
    version = "0.1.0"
    output_contract = MyOutput

    def build_prompt(self, ctx: SkillContext) -> str:
        # TODO: 领域推理指令
        return "TODO: 领域指令"


SKILLS: List[BaseSkill] = [MySkill()]

PIPELINE = Pipeline(
    name="%(name)s",
    domain="%(name)s",
    version="0.1.0",
    steps=[SkillStep(name="my_step", skill="my_skill")],
    output_synthesizer=None,
    audit_skill=None,
)


def synthesize(state, outputs) -> MyOutput:
    return outputs.get("my_step")


def register(app) -> None:
    app.register_domain(
        name="%(name)s",
        input_contract=MyInput,
        pipeline=PIPELINE,
        skills=SKILLS,
        preprocessor=MyPreprocessor,
        synthesizer=synthesize,
    )
'''

_SCAFFOLD_YAML = """name: %(name)s
domain: %(name)s
version: "0.1.0"
steps:
  - name: my_step
    type: skill
    skill: my_skill
"""


def cmd_serve(args: argparse.Namespace) -> int:
    """启动 HTTP 网关 + H5 审核台（移动端经卡片链接直达，无需安装 App）。"""
    try:
        import uvicorn
    except ImportError:
        print("缺少依赖：pip install \"vaf[api]\"（fastapi + uvicorn）", file=sys.stderr)
        return 2

    from .api.auth import ApiKeyStore
    from .api.cards import card_sink
    from .api.gateway import create_gateway
    from .runtime.notification import NotificationHub

    webhook = getattr(args, "webhook", None)
    notification_hub = None
    if webhook or args.notify_card:
        sinks = []
        if webhook:
            from .runtime.notification import webhook_sink

            sinks.append(webhook_sink(webhook, secret=args.notify_secret))
        for spec in args.notify_card or []:
            platform, _, url = spec.partition("=")
            sinks.append(
                card_sink(
                    platform.strip(),
                    url.strip(),
                    review_base=args.review_base or f"http://{args.host}:{args.port}",
                    secret=args.notify_secret,
                )
            )
        notification_hub = NotificationHub(sinks=sinks)

    app = build_app(
        sqlite_path=args.state or ".vaf/state.db",
        webhook_url=None,  # 卡片/webhook 已通过 notification_hub 挂接
        notification_hub=notification_hub,
        **_llm_kwargs(args),
    )
    keys = ApiKeyStore.from_specs(args.api_key) if args.api_key else None
    gateway = create_gateway(
        app, keys=keys, review_base=args.review_base or f"http://{args.host}:{args.port}"
    )
    mode = "开放模式（仅限内网/演示）" if keys is None else f"鉴权模式（{len(args.api_key)} 把 Key）"
    print(f"VAF 网关启动：http://{args.host}:{args.port}  （{mode}）")
    print(f"  审核台（手机可直接访问）：http://{args.host}:{args.port}/")
    print(f"  API 文档：http://{args.host}:{args.port}/docs")
    uvicorn.run(gateway, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_preset_list(args: argparse.Namespace) -> int:
    from .presets import load_index

    entries = load_index(args.presets_dir)
    print(f"预置领域（{len(entries)} 个，vaf preset try <name> 离线体验 / install 安装）\n")
    for entry in entries:
        variants = "+".join(entry.get("variants") or ["lowcode"])
        print(f"  {entry['name']:<28} [{variants}]")
        print(f"    {entry['description']}")
        knowledge = (entry.get("knowledge") or "").strip().replace("\n", " ")
        print(f"    知识依据: {knowledge[:120]}{'…' if len(knowledge) > 120 else ''}\n")
    return 0


def cmd_preset_show(args: argparse.Namespace) -> int:
    from .presets import preset_path

    path = preset_path(args.name, args.presets_dir)
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_preset_install(args: argparse.Namespace) -> int:
    from .presets import install_preset, preset_path

    dest = install_preset(args.name, target_dir=args.dir, explicit_dir=args.presets_dir)
    print(f"已安装: {dest}")
    print("下一步：")
    print(f"  1. vaf run --domain {args.name} --input <你的输入.json>   # 配置真实 LLM 后即用")
    print(f"  2. 按需编辑阈值/提示词（文件头注释标注了知识依据与校准要求）")
    from .presets import package_dir, preset_kind

    if preset_kind(args.name, args.presets_dir) == "python":
        print("  （Python 变体：包目录已复制，domains/ 自动发现后 import 即注册）")
    else:
        twin = package_dir(args.name, args.presets_dir)
        if twin.is_dir():
            print(f"  3. 代码风格参考实现: {twin}")
    return 0


def cmd_preset_try(args: argparse.Namespace) -> int:
    from .presets import try_preset

    results = try_preset(args.name, presets_root=args.presets_dir)
    print(f"== 预置领域 {args.name} 离线体验（{len(results)} 个样例，零外部依赖）==\n")
    failed = 0
    for sample_name, handle in results:
        result = handle.result or {}
        print(f"  [{sample_name}]")
        print(f"    终态: {handle.status.value}  结论: {result.get('conclusion')}")
        if handle.error:
            print(f"    错误: {handle.error[:160]}")
            failed += 1
        extra = {k: v for k, v in result.items()
                 if k in ("dti_computed", "top_risks", "missing_items", "revision_warning", "risk_factors")}
        if extra:
            print(f"    要点: {json.dumps(extra, ensure_ascii=False, default=str)[:200]}")
        if handle.falsification_flags:
            print(f"    证伪触发: {handle.falsification_flags}")
        print()
    return 1 if failed else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="vaf", description="VAF 2.0 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="离线演示两个示例领域全流程")
    demo.set_defaults(func=cmd_demo)

    run = sub.add_parser("run", help="执行一次 run")
    run.add_argument("--domain", required=True)
    run.add_argument("--input", required=True, help="输入 JSON 文件")
    run.add_argument("--request-id", default=None)
    run.add_argument("--state", default=None, help="SQLite 状态库路径")
    run.add_argument("--webhook", default=None, help="审核/澄清事件推送地址")
    _add_llm_args(run)
    run.set_defaults(func=cmd_run)

    inspect = sub.add_parser("inspect", help="查看 run 的步骤状态/路由/暂停点/事件")
    inspect.add_argument("request_id")
    inspect.add_argument("--state", default=None)
    inspect.add_argument("--json", action="store_true", help="输出完整 JSON")
    inspect.set_defaults(func=cmd_inspect)

    review = sub.add_parser("review", help="人工审核")
    review.add_argument("action", choices=["list", "approve", "reject"])
    review.add_argument("task_id", nargs="?", default=None)
    review.add_argument("--reviewer", default=None)
    review.add_argument("--comment", default="")
    review.add_argument("--state", default=None)
    review.set_defaults(func=cmd_review)

    clarify = sub.add_parser("clarify", help="澄清问答")
    clarify_sub = clarify.add_subparsers(dest="action", required=True)
    submit = clarify_sub.add_parser("submit", help="提交澄清回答")
    submit.add_argument("run_id")
    submit.add_argument("answers", help="回答 JSON 文件")
    submit.add_argument("--state", default=None)
    submit.set_defaults(func=cmd_clarify, action="submit")

    ev = sub.add_parser("eval", help="黄金集评估")
    ev.add_argument("--domain", required=True)
    ev.add_argument("--dataset", required=True, help="黄金集 YAML")
    ev.add_argument("--gates", nargs="*", default=None, help="门禁指标名，如 conclusion_accuracy")
    ev.add_argument("--state", default=None)
    _add_llm_args(ev)
    ev.set_defaults(func=cmd_eval)

    scaffold = sub.add_parser("scaffold", help="生成领域模板（默认低代码 YAML）")
    scaffold.add_argument("name")
    scaffold.add_argument("--dir", default=None)
    scaffold.add_argument("--force", action="store_true")
    scaffold.add_argument(
        "--style", choices=["lowcode", "code"], default="lowcode",
        help="lowcode=单个 YAML 文件（默认）；code=Python 领域包（完整控制）",
    )
    scaffold.set_defaults(func=cmd_scaffold)

    serve = sub.add_parser("serve", help="启动 HTTP 网关 + H5 审核台（移动接入）")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--state", default=None, help="SQLite 状态库路径（默认 .vaf/state.db）")
    serve.add_argument("--api-key", action="append", default=None,
                       help="API Key 配置，格式 key:user_id:role1,role2（可重复；不配置=开放模式）")
    serve.add_argument("--notify-card", action="append", default=None,
                       help="消息卡片推送：dingtalk=URL / wecom=URL / feishu=URL（可重复）")
    serve.add_argument("--notify-secret", default=None, help="钉钉/飞书机器人加签密钥")
    serve.add_argument("--webhook", default=None, help="原始 JSON webhook（与 --notify-card 可同时用）")
    serve.add_argument("--review-base", default=None, help="卡片/审核台对外可达基址（默认 http://host:port）")
    serve.add_argument("--log-level", default="info")
    _add_llm_args(serve)
    serve.set_defaults(func=cmd_serve)

    preset = sub.add_parser("preset", help="预置领域库（0 分钟即用）")
    preset_sub = preset.add_subparsers(dest="action", required=True)

    p_list = preset_sub.add_parser("list", help="列出预置领域")
    p_list.add_argument("--presets-dir", default=None)
    p_list.set_defaults(func=cmd_preset_list)

    p_show = preset_sub.add_parser("show", help="查看预置领域 YAML 全文")
    p_show.add_argument("name")
    p_show.add_argument("--presets-dir", default=None)
    p_show.set_defaults(func=cmd_preset_show)

    p_install = preset_sub.add_parser("install", help="安装到 domains/（即被自动发现）")
    p_install.add_argument("name")
    p_install.add_argument("--dir", default="domains")
    p_install.add_argument("--presets-dir", default=None)
    p_install.set_defaults(func=cmd_preset_install)

    p_try = preset_sub.add_parser("try", help="离线体验（样例输入 + 脚本化响应）")
    p_try.add_argument("name")
    p_try.add_argument("--presets-dir", default=None)
    p_try.set_defaults(func=cmd_preset_try)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
