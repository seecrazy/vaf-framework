"""步骤处理器（DESIGN §7.12，V2.1）：编排引擎的开闭式扩展点。

每种步骤类型（skill / human_review / interaction / router / sub_pipeline /
generation / …）由一个 StepHandler 实现；引擎只按 step_type 查表分发，
新增步骤类型 = 注册一个处理器，零引擎改动。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel

from ..errors import PipelineValidationError, StepFailure
from .state import PipelineState, RunStatus, StepState, StepStatusEnum
from .steps import (
    GenerationStep,
    HumanReviewStep,
    InteractionStep,
    Pipeline,
    RouterStep,
    SkillStep,
    SubPipelineStep,
)


class StepRunContext:
    """处理器执行上下文：引擎共享基础设施 + 单步数据。"""

    __slots__ = ("engine", "state", "reg", "step", "typed", "ss", "run_span")

    def __init__(self, engine, state: PipelineState, reg, step, typed: Dict[str, Any], ss: StepState, run_span):
        self.engine = engine
        self.state = state
        self.reg = reg
        self.step = step
        self.typed = typed
        self.ss = ss
        self.run_span = run_span


class StepHandler:
    """步骤处理器基类：子类声明 step_type 并实现 run/validate。"""

    step_type: Type[BaseModel]

    def validate(self, step: Any, pipeline: Pipeline, reg, engine) -> None:
        """加载期校验钩子（默认无额外校验）。"""

    @staticmethod
    def require_skill(reg, step_name: str, skill_name: str) -> Any:
        skill = reg.skills.get(skill_name)
        if skill is None:
            raise PipelineValidationError(
                f"step '{step_name}' references unregistered skill '{skill_name}'"
            )
        return skill

    def run(self, ctx: StepRunContext) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


# ---------------------------------------------------------------------------
# skill 步骤
# ---------------------------------------------------------------------------
class SkillStepHandler(StepHandler):
    step_type = SkillStep

    def validate(self, step, pipeline, reg, engine) -> None:
        self.require_skill(reg, step.name, step.skill)

    def run(self, ctx: StepRunContext) -> bool:
        step: SkillStep = ctx.step
        state, reg, typed, ss = ctx.state, ctx.reg, ctx.typed, ctx.ss
        skill = reg.skills[step.skill]
        attempts_allowed = step.max_retries + 1 if step.on_schema_fail == "retry" else 1
        last_error: Optional[Exception] = None
        for attempt in range(attempts_allowed):
            ss.attempts = attempt + 1
            try:
                output = self._execute_with_loop(ctx, skill)
                ss.output = output.model_dump(mode="json")
                ss.output_contract = type(output).__name__
                typed[step.name] = output
                ss.status = StepStatusEnum.SUCCEEDED
                ctx.engine._check_falsification_realtime(state, step.name, output)
                return False
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                ctx.engine._emit(
                    "skill_attempt_failed",
                    request_id=state.request_id,
                    step=step.name,
                    attempt=attempt + 1,
                    error=str(exc)[:200],
                )
                if attempt >= attempts_allowed - 1:
                    break
        if step.on_schema_fail == "skip":
            ss.status = StepStatusEnum.SKIPPED
            ss.degraded_reason = f"schema fail skipped: {last_error}"
            return False
        if step.on_schema_fail == "degrade":
            ss.status = StepStatusEnum.DEGRADED
            ss.degraded_reason = f"degraded after failure: {last_error}"
            ss.output = None
            return False
        raise StepFailure(step.name, f"skill failed after {ss.attempts} attempts: {last_error}")

    def _execute_with_loop(self, ctx: StepRunContext, skill) -> BaseModel:
        engine, step, ss = ctx.engine, ctx.step, ctx.ss
        output = engine.executor.execute(skill, engine._build_ctx(ctx.state, ctx.reg, step, ctx.typed, ss))
        if not step.loop_condition:
            return output
        ctx.typed[step.name] = output
        ss.output = output.model_dump(mode="json")
        for _ in range(step.max_loops):
            ns = engine._namespace(ctx.state, ctx.typed)
            ns["loop_count"] = ss.loop_count + 1
            if not engine._eval(step.loop_condition, ns, f"loop:{step.name}"):
                break
            ss.loop_count += 1
            output = engine.executor.execute(
                skill, engine._build_ctx(ctx.state, ctx.reg, step, ctx.typed, ss)
            )
            ctx.typed[step.name] = output
            ss.output = output.model_dump(mode="json")
        return output


# ---------------------------------------------------------------------------
# 人工审核步骤
# ---------------------------------------------------------------------------
class HumanReviewHandler(StepHandler):
    step_type = HumanReviewStep

    def run(self, ctx: StepRunContext) -> bool:
        engine, step, ss = ctx.engine, ctx.step, ctx.ss
        ns = engine._namespace(ctx.state, ctx.typed)
        if not engine._eval(step.trigger_condition, ns, f"trigger:{step.name}"):
            ss.status = StepStatusEnum.SKIPPED
            ss.degraded_reason = "trigger_condition false"
            return False

        info = next((r for r in reversed(ctx.state.reviews) if r.step == step.name), None)
        if info is not None:
            if info.status == "approved":
                ss.status = StepStatusEnum.SUCCEEDED
                return False
            if info.status == "rejected":
                raise StepFailure(
                    step.name, f"human review rejected: {info.comment or 'no comment'}"
                )
            if info.status == "timeout":
                return self._apply_timeout(ctx, step.on_timeout)
            ctx.state.pending_review_task = info.task_id
            ctx.state.status = RunStatus.WAITING_REVIEW
            return True

        task = engine.reviews.submit(
            run_id=ctx.state.request_id,
            step=step.name,
            pipeline=ctx.state.pipeline,
            review_fields=step.review_fields,
            payload_digest=self._digest(ctx),
            required_role=step.required_role,
            on_timeout=step.on_timeout,
            timeout_seconds=step.timeout_seconds,
        )
        from .state import ReviewInfo

        ctx.state.reviews.append(ReviewInfo(task_id=task.task_id, step=step.name, status="pending"))
        ctx.state.pending_review_task = task.task_id
        ctx.state.status = RunStatus.WAITING_REVIEW
        engine._emit(
            "review_requested",
            request_id=ctx.state.request_id,
            step=step.name,
            task_id=task.task_id,
            fields=step.review_fields,
        )
        return True

    def _apply_timeout(self, ctx: StepRunContext, action: str) -> bool:
        engine, step, ss = ctx.engine, ctx.step, ctx.ss
        engine._emit("review_timeout", request_id=ctx.state.request_id, step=step.name, action=action)
        if action in ("pass", "degrade"):
            ss.status = StepStatusEnum.DEGRADED
            ss.degraded_reason = f"review timeout, on_timeout={action}"
            return False
        raise StepFailure(step.name, "review timeout with on_timeout=reject")

    @staticmethod
    def _digest(ctx: StepRunContext) -> Dict[str, object]:
        step: HumanReviewStep = ctx.step
        digest: Dict[str, object] = {}
        for dep in step.depends_on:
            out = ctx.state.step_output(dep)
            if isinstance(out, dict):
                for f in step.review_fields:
                    if f in out:
                        digest[f] = out[f]
        return digest


# ---------------------------------------------------------------------------
# 交互澄清步骤
# ---------------------------------------------------------------------------
class InteractionStepHandler(StepHandler):
    step_type = InteractionStep

    def validate(self, step, pipeline, reg, engine) -> None:
        from ..skill.base import AnalysisSkill

        skill = self.require_skill(reg, step.name, step.skill)
        if not isinstance(skill, AnalysisSkill):
            raise PipelineValidationError(
                f"interaction step '{step.name}' requires an AnalysisSkill"
            )

    def run(self, ctx: StepRunContext) -> bool:
        engine, step, ss = ctx.engine, ctx.step, ctx.ss
        skill = ctx.reg.skills[step.skill]

        pending = ctx.state.pending_clarification
        if pending is not None and pending.answers:
            clar = dict(ctx.state.variables.get("clarifications", {}))
            clar.update(pending.answers)
            ctx.state.variables["clarifications"] = clar
            ctx.state.clarifications.append(pending)
            ctx.state.pending_clarification = None
            ss.loop_count += 1

        from ..contract.base import is_insufficient

        output = engine.executor.execute(
            skill, engine._build_ctx(ctx.state, ctx.reg, step, ctx.typed, ss)
        )
        ns = engine._namespace(ctx.state, ctx.typed)
        needs_input = is_insufficient(output.conclusion)
        if step.trigger_condition:
            needs_input = needs_input or engine._eval(
                step.trigger_condition, ns, f"trigger:{step.name}"
            )

        if not needs_input:
            ss.output = output.model_dump(mode="json")
            ss.output_contract = type(output).__name__
            ctx.typed[step.name] = output
            ss.status = StepStatusEnum.SUCCEEDED
            return False

        if ss.loop_count >= step.max_rounds:
            if step.on_exhausted == "skip":
                ss.status = StepStatusEnum.SKIPPED
                ss.degraded_reason = f"clarification exhausted after {ss.loop_count} rounds"
                return False
            if step.on_exhausted == "degrade":
                ss.status = StepStatusEnum.DEGRADED
                ss.degraded_reason = f"clarification exhausted after {ss.loop_count} rounds"
                ss.output = output.model_dump(mode="json")
                return False
            raise StepFailure(step.name, f"clarification exhausted after {ss.loop_count} rounds")

        questions = list(output.clarifying_questions)
        if not questions:
            raise StepFailure(
                step.name, "insufficient_data output must provide clarifying_questions"
            )
        from .state import ClarificationInfo

        ctx.state.pending_clarification = ClarificationInfo(
            round_no=ss.loop_count + 1, questions=questions
        )
        ctx.state.status = RunStatus.WAITING_INPUT
        engine._emit(
            "clarification_requested",
            request_id=ctx.state.request_id,
            step=step.name,
            round_no=ctx.state.pending_clarification.round_no,
            questions=questions,
        )
        return True


# ---------------------------------------------------------------------------
# 路由步骤
# ---------------------------------------------------------------------------
class RouterStepHandler(StepHandler):
    step_type = RouterStep

    def validate(self, step, pipeline, reg, engine) -> None:
        names = {s.name for s in pipeline.steps}
        for route in step.routes:
            if route.go_to not in names:
                raise PipelineValidationError(
                    f"router '{step.name}' routes to unknown step '{route.go_to}'"
                )
        if step.default_next is not None and step.default_next not in names:
            raise PipelineValidationError(
                f"router '{step.name}' default_next points to unknown step '{step.default_next}'"
            )

    def run(self, ctx: StepRunContext) -> bool:
        engine, step, ss = ctx.engine, ctx.step, ctx.ss
        ns = engine._namespace(ctx.state, ctx.typed)
        selected: Optional[str] = None
        for route in step.routes:
            if engine._eval(route.condition, ns, f"route:{step.name}"):
                selected = route.go_to
                break
        if selected is None:
            selected = step.default_next
        ctx.state.routers[step.name] = selected or ""
        ss.output = {"selected": selected}
        ss.output_contract = "RouterDecision"
        ss.status = StepStatusEnum.SUCCEEDED
        engine._emit(
            "router_selected", request_id=ctx.state.request_id, router=step.name, selected=selected
        )
        return False


# ---------------------------------------------------------------------------
# 子流程步骤
# ---------------------------------------------------------------------------
class SubPipelineHandler(StepHandler):
    step_type = SubPipelineStep

    def validate(self, step, pipeline, reg, engine) -> None:
        if step.pipeline == pipeline.name:
            raise PipelineValidationError(
                f"sub_pipeline '{step.name}' cannot reference itself"
            )
        target = engine._domains.get(step.pipeline)
        if target is not None:
            engine._validate_sub_target(step, target.pipeline)

    def run(self, ctx: StepRunContext) -> bool:
        engine, step, ss = ctx.engine, ctx.step, ctx.ss
        target = engine._require_domain(step.pipeline)
        engine._validate_sub_target(step, target.pipeline)
        child_request = f"{ctx.state.request_id}:{step.name}"
        child_input_data: Dict[str, Any] = {}
        if step.input_from:
            source = ctx.state.step_output(step.input_from)
            if isinstance(source, dict):
                child_input_data = dict(source)
            elif source is not None:
                child_input_data = {"payload": source}
        child_input = target.input_contract.model_validate(child_input_data)
        from ..errors import IdempotencyConflictError

        try:
            handle = engine.run(step.pipeline, child_input, child_request)
        except IdempotencyConflictError:
            handle = engine.resume(child_request)  # 子流程此前失败：断点重试
        from .state import RunStatus as _RS

        if handle.status == _RS.SUCCEEDED:
            ss.output = {"result": handle.result, "child_request_id": child_request}
            ss.output_contract = "SubPipelineResult"
            ctx.typed[step.name] = ss.output
            ss.status = StepStatusEnum.SUCCEEDED
            return False
        raise StepFailure(step.name, f"sub-pipeline status={handle.status.value}: {handle.error}")


# ---------------------------------------------------------------------------
# 生成（流式）步骤 —— V2.1 新增：注册式扩展的第一个受益者
# ---------------------------------------------------------------------------
class GenerationStepHandler(StepHandler):
    step_type = GenerationStep

    def validate(self, step, pipeline, reg, engine) -> None:
        from ..skill.base import GenerationSkill

        skill = self.require_skill(reg, step.name, step.skill)
        if not isinstance(skill, GenerationSkill):
            raise PipelineValidationError(
                f"generation step '{step.name}' requires a GenerationSkill"
            )

    def run(self, ctx: StepRunContext) -> bool:
        from ..errors import StreamViolationError
        from ..skill.base import TextOutput

        engine, step, ss = ctx.engine, ctx.step, ctx.ss
        skill = ctx.reg.skills[step.skill]
        streamed: List[str] = []

        def on_token(token: str) -> None:
            streamed.append(token)

        output = engine.executor.stream_execute(
            skill,
            engine._build_ctx(ctx.state, ctx.reg, step, ctx.typed, ss),
            on_token,
            checkpoint_size=step.checkpoint_size,
            holdback_size=step.holdback_size,
            on_violation="abort",
        )
        text = getattr(output, "text", "".join(streamed))
        ss.output = (
            output.model_dump(mode="json")
            if isinstance(output, BaseModel)
            else {"text": text}
        )
        ss.output_contract = type(output).__name__ if isinstance(output, BaseModel) else "TextOutput"
        ctx.typed[step.name] = output
        if getattr(output, "degraded", False):
            ss.status = StepStatusEnum.DEGRADED
            ss.degraded_reason = "streaming compliance flags: " + ",".join(
                getattr(output, "compliance_flags", [])[:3]
            )
        else:
            ss.status = StepStatusEnum.SUCCEEDED
        engine._emit(
            "generation_streamed",
            request_id=ctx.state.request_id,
            step=step.name,
            chars=len(text),
            preview=text[:80],
        )
        return False


DEFAULT_HANDLERS: List[StepHandler] = [
    SkillStepHandler(),
    HumanReviewHandler(),
    InteractionStepHandler(),
    RouterStepHandler(),
    SubPipelineHandler(),
    GenerationStepHandler(),
]
