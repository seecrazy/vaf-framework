"""评估执行器（DESIGN §13）：批量跑黄金集 → 结构化指标 → 发布门禁。

评估 run 全部走正式引擎路径（契约校验/证伪/幂等一视同仁），
request_id 形如 eval:{dataset}:{case_id}，重复评估自动幂等命中缓存。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..contract.base import INSUFFICIENT_DATA, is_insufficient
from ..runtime.engine import PipelineEngine, RunHandle
from .dataset import GoldenCase, GoldenDataset

import time


class Gate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str  # conclusion_accuracy | contract_pass_rate | falsification_rate
    op: str = ">="  # >= | >
    threshold: float

    def passed(self, value: float) -> bool:
        if self.op == ">=":
            return value >= self.threshold
        if self.op == ">":
            return value > self.threshold
        raise ValueError(f"unsupported gate op: {self.op}")


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    status: str  # succeeded | failed | waiting
    conclusion: Optional[str] = None
    expected_conclusion: Optional[str] = None
    correct: Optional[bool] = None
    falsification_provided: bool = False
    sentinel: bool = False
    error: str = ""
    duration_s: float = 0.0


class EvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    total: int
    metrics: Dict[str, float] = Field(default_factory=dict)
    gate_passed: bool = True
    failed_gates: List[str] = Field(default_factory=list)
    cases: List[CaseResult] = Field(default_factory=list)
    evaluated_at: float = Field(default_factory=time.time)


class EvalRunner:
    def __init__(
        self,
        engine: PipelineEngine,
        *,
        auto_clarify: bool = True,
        auto_approve: bool = True,
        max_auto_rounds: int = 3,
    ):
        """auto_clarify / auto_approve：评估需无人值守跑完——澄清自动提交默认肯定回答；
    审核用评估员角色自动通过（生产评估建议在黄金集中规避触发审核的用例或显式记录）。"""
        self.engine = engine
        self.auto_clarify = auto_clarify
        self.auto_approve = auto_approve
        self.max_auto_rounds = max_auto_rounds

    def run(
        self,
        dataset: GoldenDataset,
        *,
        request_prefix: str = "eval",
        gates: Optional[List[Gate]] = None,
        progress: Optional[Callable[[CaseResult], None]] = None,
    ) -> EvalReport:
        cases: List[CaseResult] = []
        for case in dataset.cases:
            result = self._run_case(dataset.domain, case, request_prefix)
            cases.append(result)
            if progress:
                progress(result)

        metrics = self._compute_metrics(cases)
        gates = gates or []
        failed_gates = [
            f"{g.metric} {g.op} {g.threshold} (actual {metrics.get(g.metric, 0.0):.4f})"
            for g in gates
            if not g.passed(metrics.get(g.metric, 0.0))
        ]
        return EvalReport(
            domain=dataset.domain,
            total=len(cases),
            metrics=metrics,
            gate_passed=not failed_gates,
            failed_gates=failed_gates,
            cases=cases,
        )

    # ------------------------------------------------------------------
    def _run_case(self, domain: str, case: GoldenCase, prefix: str) -> CaseResult:
        reg = self.engine.domain_of(domain)
        if reg is None:
            return CaseResult(
                case_id=case.case_id, status="failed", error=f"unknown pipeline: {domain}"
            )
        started = time.time()
        request_id = f"{prefix}:{domain}:{case.case_id}"
        try:
            domain_input = reg.input_contract.model_validate(case.input)
            from ..errors import IdempotencyConflictError

            try:
                handle: RunHandle = self.engine.run(domain, domain_input, request_id)
            except IdempotencyConflictError:
                # 同 request_id 的历史失败 run：从失败步骤断点重试
                handle = self.engine.resume(request_id)
            # 无人值守处理暂停点（澄清 / 审核）
            for _ in range(self.max_auto_rounds):
                if handle.status.value == "waiting_input" and self.auto_clarify:
                    answers = {"默认": "是"}
                    handle = self.engine.submit_clarification(request_id, answers)
                    continue
                if handle.status.value == "waiting_review" and self.auto_approve:
                    from ..security.permissions import User

                    reviewer = User("eval-auto", {"reviewer", "operator"})
                    handle = self.engine.submit_review(
                        handle.pending_review_task, "approved", reviewer=reviewer,
                        comment="eval auto-approve",
                    )
                    continue
                break
        except Exception as exc:  # noqa: BLE001
            return CaseResult(
                case_id=case.case_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                duration_s=round(time.time() - started, 4),
            )
        duration = round(time.time() - started, 4)
        if handle.status.value == "succeeded" and isinstance(handle.result, dict):
            conclusion = handle.result.get("conclusion")
            sentinel = conclusion == INSUFFICIENT_DATA or is_insufficient(conclusion or "")
            # 证伪提供率读步骤输出（合成结果结构因领域而异）
            falsification_provided = False
            state = self.engine.store.get(request_id)
            if state is not None:
                falsification_provided = any(
                    isinstance(ss.output, dict) and ss.output.get("falsification")
                    for ss in state.step_states.values()
                )
            correct = (
                case.expected_conclusion is None or conclusion == case.expected_conclusion
            )
            return CaseResult(
                case_id=case.case_id,
                status="succeeded",
                conclusion=str(conclusion) if conclusion is not None else None,
                expected_conclusion=case.expected_conclusion,
                correct=correct,
                falsification_provided=falsification_provided,
                sentinel=sentinel,
                duration_s=duration,
            )
        return CaseResult(
            case_id=case.case_id,
            status="waiting" if handle.status.value.startswith("waiting") else "failed",
            error=handle.error,
            duration_s=duration,
        )

    @staticmethod
    def _compute_metrics(cases: List[CaseResult]) -> Dict[str, float]:
        if not cases:
            return {"conclusion_accuracy": 0.0, "contract_pass_rate": 0.0, "falsification_rate": 0.0}
        succeeded = [c for c in cases if c.status == "succeeded"]
        scored = [c for c in succeeded if c.correct is not None]
        accuracy = (
            sum(1 for c in scored if c.correct) / len(scored) if scored else 0.0
        )
        contract_pass = len(succeeded) / len(cases)
        non_sentinel = [c for c in succeeded if not c.sentinel]
        falsification_rate = (
            sum(1 for c in non_sentinel if c.falsification_provided) / len(non_sentinel)
            if non_sentinel
            else 1.0
        )
        return {
            "conclusion_accuracy": round(accuracy, 4),
            "contract_pass_rate": round(contract_pass, 4),
            "falsification_rate": round(falsification_rate, 4),
        }
