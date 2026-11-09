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


def build_app(sqlite_path: Optional[str] = None, webhook_url: Optional[str] = None) -> VafApp:
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

    notification_hub = None
    if webhook_url:
        from .runtime.notification import NotificationHub

        notification_hub = NotificationHub(sinks=[webhook_sink(webhook_url)])

    app = VafApp(sqlite_path=sqlite_path, notification_hub=notification_hub)
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

    # 场景 3：评估 + 门禁
    print("[3] 评估闭环：黄金集跑分 + 门禁")
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
    app = build_app(sqlite_path=args.state or None, webhook_url=getattr(args, "webhook", None))
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
    app = build_app(sqlite_path=args.state or None)
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
    base = os.path.join(args.dir or "domains", name)
    os.makedirs(base, exist_ok=True)
    files = {
        "__init__.py": _SCAFFOLD_INIT % {"name": name},
        "pipeline.yaml": _SCAFFOLD_YAML % {"name": name},
        "test_cases.yaml": "domain: %s\ncases: []\n" % name,
    }
    for fname, content in files.items():
        path = os.path.join(base, fname)
        if os.path.exists(path) and not args.force:
            print(f"跳过已存在: {path}")
            continue
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"生成: {path}")
    return 0


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
    ev.set_defaults(func=cmd_eval)

    scaffold = sub.add_parser("scaffold", help="生成领域包模板")
    scaffold.add_argument("name")
    scaffold.add_argument("--dir", default=None)
    scaffold.add_argument("--force", action="store_true")
    scaffold.set_defaults(func=cmd_scaffold)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
