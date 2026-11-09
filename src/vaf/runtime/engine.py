"""编排引擎（DESIGN §7.11 / §7.12）：拓扑调度 + 并行组 + 条件/循环 + 暂停点 + 快照 + 幂等 + 补偿。

V2.1 修订：步骤执行与类型校验委托给 StepHandler 注册表（handlers.py）——
引擎对"新增步骤类型"开闭：engine.register_step_handler(...) 即可，零引擎改动。

暂停点语义：
- WAITING_REVIEW：人工审核任务待决策（app.submit_review 后自动 resume）
- WAITING_INPUT：澄清问题待回答（app.submit_clarification 后自动 resume）
断点续跑：SUCCEEDED/DEGRADED/SKIPPED 步骤直接跳过；FAILED run 可 resume 重试；
执行计划快照不一致即 PlanMismatchError（A3）。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel

from ..config.center import ConfigCenter
from ..contract.base import DomainInput, SkillOutput
from ..contract.falsification import FalsificationEngine
from ..errors import (
    ExpressionError,
    IdempotencyConflictError,
    PermissionDeniedError,
    PipelineValidationError,
    PlanMismatchError,
    ReviewError,
    StepFailure,
    VafError,
)
from ..observe.events import EventBus
from ..observe.metrics import Clock, MetricsRegistry, default_metrics
from ..observe.trace import Span, TraceCollector
from ..security.audit import AuditLog
from ..security.permissions import PermissionChecker, User
from ..skill.base import AuditResult, AuditSkill, BaseSkill
from ..skill.context import DEFAULT_STATE_ACL, SkillContext, StateView
from ..skill.executor import SkillExecutor
from ..skill.memory import ErrorMemory
from .expression import SafeASTExpressionEngine
from .handlers import DEFAULT_HANDLERS, StepHandler, StepRunContext
from .persistence import StateStore
from .review import ReviewQueue
from .snapshot import ExecutionPlan, build_plan
from .state import PipelineState, RunStatus, StepState, StepStatusEnum
from .steps import Pipeline, RouterStep, detect_cycle

SynthesizerFn = Callable[[PipelineState, Dict[str, Any]], BaseModel]

__all__ = [
    "DomainRegistration", "PipelineEngine", "RunHandle", "StepFailure",
    "StepHandler", "StepRunContext",
]


@dataclass
class DomainRegistration:
    name: str
    input_contract: Type[DomainInput]
    pipeline: Pipeline
    skills: Dict[str, BaseSkill]
    preprocessor: Optional[Callable[[], Any]] = None
    synthesizer: Optional[SynthesizerFn] = None
    audit_skill: Optional[str] = None
    data_classification: str = "internal"


@dataclass
class RunHandle:
    request_id: str
    status: RunStatus
    result: Optional[Any] = None
    error: str = ""
    pending_review_task: Optional[str] = None
    pending_clarification: Optional[Any] = None
    falsification_flags: List[str] = field(default_factory=list)
    plan_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "pending_review_task": self.pending_review_task,
            "pending_clarification": (
                self.pending_clarification.model_dump(mode="json")
                if self.pending_clarification
                else None
            ),
            "falsification_flags": list(self.falsification_flags),
            "plan_hash": self.plan_hash,
        }

    @classmethod
    def from_state(cls, state: PipelineState) -> "RunHandle":
        return cls(
            request_id=state.request_id,
            status=state.status,
            result=state.result,
            error=state.error,
            pending_review_task=state.pending_review_task,
            pending_clarification=state.pending_clarification,
            falsification_flags=list(state.falsification_flags),
            plan_hash=state.plan_hash,
        )


class PipelineEngine:
    def __init__(
        self,
        *,
        state_store: StateStore,
        review_queue: ReviewQueue,
        executor: SkillExecutor,
        falsification: FalsificationEngine,
        error_memory: ErrorMemory,
        config_center: ConfigCenter,
        events: Optional[EventBus] = None,
        metrics: Optional[MetricsRegistry] = None,
        traces: Optional[TraceCollector] = None,
        audit: Optional[AuditLog] = None,
        permissions: Optional[PermissionChecker] = None,
        clock: Clock = time.time,
        tenant: str = "default",
    ):
        self.store = state_store
        self.reviews = review_queue
        self.executor = executor
        self.falsification = falsification
        self.error_memory = error_memory
        self.config = config_center
        self.events = events
        self.metrics = metrics or default_metrics()
        self.traces = traces
        self.audit = audit
        self.permissions = permissions or PermissionChecker()
        self.clock = clock
        self.tenant = tenant
        self.expr = SafeASTExpressionEngine()
        self._domains: Dict[str, DomainRegistration] = {}  # pipeline name → registration
        self._run_total = self.metrics.counter("runs_total", "run 总数")
        self._run_result = self.metrics.counter("run_result", "run 终态分布")
        self._step_duration = self.metrics.histogram("step_duration_s", "步骤耗时（按类型分桶）")
        # 运行期类型化输出缓存 request_id → {step → BaseModel|dict}（LRU，防长期运行膨胀）
        self._typed: Dict[str, Dict[str, Any]] = {}
        self._typed_lock = threading.Lock()
        self._typed_capacity = 256
        self._typed_order: List[str] = []  # 访问序（尾部最新）
        # 副作用补偿登记 pending 队列（工具线程写入，随 _persist 合并进状态）
        self._pending_side_effects: Dict[str, List] = {}
        self._side_effects_lock = threading.Lock()
        self.executor.tools._compensation_registry = self._record_side_effect
        # 步骤处理器注册表（V2.1 开闭式扩展点）
        self._step_handlers: Dict[Type, StepHandler] = {}
        for handler in DEFAULT_HANDLERS:
            self.register_step_handler(handler)

    # ==================================================================
    # 步骤处理器注册表（§7.12）
    # ==================================================================
    def register_step_handler(self, handler: StepHandler) -> None:
        if not getattr(handler, "step_type", None):
            raise VafError(f"step handler {type(handler).__name__} must declare step_type")
        self._step_handlers[handler.step_type] = handler

    def handler_for(self, step: Any) -> StepHandler:
        for cls in type(step).__mro__:
            if cls in self._step_handlers:
                return self._step_handlers[cls]
        if type(step).__name__ == "BaseModel":
            raise VafError(
                f"step '{getattr(step, 'name', '?')}' has an unregistered type tag; "
                f"custom step types must be constructed in Python and registered via "
                f"engine.register_step_handler()"
            )
        raise VafError(
            f"no StepHandler registered for step type {type(step).__name__}; "
            f"register one via engine.register_step_handler()"
        )

    def _evict_typed_locked(self) -> None:
        """持锁调用：超出容量时淘汰最旧条目（等待中的 run 优先保留）。"""
        while len(self._typed_order) > self._typed_capacity:
            oldest = self._typed_order.pop(0)
            self._typed.pop(oldest, None)

    def _record_side_effect(self, undo_key: str, request_id: str, tool: str, description: str) -> None:
        from .state import SideEffectRecord

        with self._side_effects_lock:
            self._pending_side_effects.setdefault(request_id, []).append(
                SideEffectRecord(
                    undo_key=undo_key, request_id=request_id, tool=tool, description=description
                )
            )

    # ==================================================================
    # 注册与校验
    # ==================================================================
    def register_domain(self, reg: DomainRegistration) -> None:
        self.validate_pipeline(reg.pipeline, reg)
        self._domains[reg.pipeline.name] = reg

    def domain_of(self, pipeline_name: str) -> Optional[DomainRegistration]:
        return self._domains.get(pipeline_name)

    def validate_pipeline(self, p: Pipeline, reg: DomainRegistration) -> None:
        names = [s.name for s in p.steps]
        if len(set(names)) != len(names):
            dupes = {n for n in names if names.count(n) > 1}
            raise PipelineValidationError(f"duplicate step names: {sorted(dupes)}")
        name_set = set(names)
        for step in p.steps:
            for dep in getattr(step, "depends_on", []) or []:  # 自定义步骤可无该字段
                if dep not in name_set:
                    raise PipelineValidationError(
                        f"step '{step.name}' depends on unknown step '{dep}'"
                    )
            # 类型专属校验委托给处理器（V2.1）
            self.handler_for(step).validate(step, p, reg, self)

        cycle = detect_cycle(p.steps)
        if cycle:
            raise PipelineValidationError(f"dependency cycle involving steps: {cycle}")

        routers = {s.name: s for s in p.steps if isinstance(s, RouterStep)}

        # 表达式预编译 + router 引用约束（属性驱动收集，仅 router 路由表需类型判断）
        for step in p.steps:
            exprs: List[Tuple[str, str]] = []
            if getattr(step, "condition", None):
                exprs.append((step.condition, "condition"))
            loop_cond = getattr(step, "loop_condition", None)
            if loop_cond:
                exprs.append((loop_cond, "loop_condition"))
            trigger = getattr(step, "trigger_condition", None)
            if trigger:
                exprs.append((trigger, "trigger_condition"))
            for route in getattr(step, "routes", []) or []:
                exprs.append((route.condition, "route"))
            for text, kind in exprs:
                self._compile_condition(text, context=f"step '{step.name}' ({kind})")
                for router_name in routers:
                    if router_name == step.name:
                        continue
                    if f"routers.{router_name}" in text:
                        if isinstance(step, RouterStep):
                            raise PipelineValidationError(
                                f"router '{step.name}' may not reference router '{router_name}'"
                            )
                        if router_name not in step.depends_on:
                            raise PipelineValidationError(
                                f"step '{step.name}' references router '{router_name}' in "
                                f"{kind} but does not depend on it"
                            )

    def _validate_sub_target(self, step, target: Pipeline) -> None:
        for t in target.steps:
            if t.type in ("human_review", "interaction"):
                raise PipelineValidationError(
                    f"sub_pipeline '{step.name}' target '{target.name}' contains pause-type "
                    f"step '{t.name}' (human_review/interaction)"
                )

    def _compile_condition(self, text: str, context: str = "") -> Any:
        try:
            return self.expr.compile(text)
        except ExpressionError as exc:
            raise PipelineValidationError(f"invalid condition ({context}): {exc}") from exc

    # ==================================================================
    # 入口
    # ==================================================================
    def run(
        self,
        pipeline_name: str,
        domain_input: DomainInput,
        request_id: str,
        user: Optional[User] = None,
    ) -> RunHandle:
        reg = self._require_domain(pipeline_name)
        existing = self.store.get(request_id)
        if existing is not None:
            if existing.status == RunStatus.SUCCEEDED:
                return RunHandle.from_state(existing)
            if not existing.status.terminal:
                return RunHandle.from_state(existing)
            raise IdempotencyConflictError(
                f"request_id '{request_id}' is in terminal state "
                f"'{existing.status.value}'; use resume('{request_id}') to retry from the "
                f"failed step, or submit with a new request_id"
            )

        plan = self._build_plan(reg)
        state = PipelineState(
            request_id=request_id,
            pipeline=pipeline_name,
            domain=reg.name,
            plan_hash=plan.plan_hash,
            status=RunStatus.RUNNING,
            input=domain_input.model_dump(mode="json"),
        )
        for step in reg.pipeline.steps:
            state.step_states[step.name] = StepState(step=step.name)
        with self._typed_lock:
            self._typed[request_id] = {}
            self._typed_order.append(request_id)
            self._evict_typed_locked()

        run_span = self._span("run", pipeline=pipeline_name, request_id=request_id)
        self._run_total.inc(pipeline=pipeline_name)
        self._persist(state)
        self._emit("run_started", request_id=request_id, pipeline=pipeline_name)

        try:
            self._run_preprocessor(state, reg)
            return self._execute(state, reg, run_span)
        except Exception as exc:  # noqa: BLE001
            return self._fail_run(state, reg, exc, run_span)

    def resume(self, request_id: str, migrate: bool = False) -> RunHandle:
        state = self.store.get(request_id)
        if state is None:
            raise VafError(f"unknown run: {request_id}")
        # SUCCEEDED/CANCELLED/INVALIDATED 直接返回；FAILED 允许断点重试
        if state.status.terminal and state.status != RunStatus.FAILED:
            return RunHandle.from_state(state)
        # FAILED 步骤重置为 PENDING，从失败点重试（幂等跳过已完成步骤）
        for ss in state.step_states.values():
            if ss.status == StepStatusEnum.FAILED:
                ss.status = StepStatusEnum.PENDING
                ss.error = ""
        reg = self._require_domain(state.pipeline)
        current_plan = self._build_plan(reg)
        if current_plan.plan_hash != state.plan_hash and not migrate:
            raise PlanMismatchError(
                f"execution plan changed since run started "
                f"(stored={state.plan_hash}, current={current_plan.plan_hash}); "
                f"pass migrate=True to continue under the new plan"
            )
        state.status = RunStatus.RUNNING
        state.error = ""
        state.touch()
        self._persist(state)
        run_span = self._span("run:resume", pipeline=state.pipeline, request_id=request_id)
        try:
            return self._execute(state, reg, run_span)
        except Exception as exc:  # noqa: BLE001
            return self._fail_run(state, reg, exc, run_span)

    def submit_review(
        self,
        task_id: str,
        decision: str,
        reviewer: Optional[User] = None,
        comment: str = "",
    ) -> RunHandle:
        task = self.reviews.get(task_id)
        if task is None:
            raise ReviewError(f"unknown review task: {task_id}")
        if task.required_role:
            if reviewer is None:
                raise PermissionDeniedError(
                    f"review task requires role '{task.required_role}' but no reviewer provided"
                )
            self.permissions.require_any_role(reviewer, [task.required_role])
        task = self.reviews.decide(
            task_id, decision, reviewer.user_id if reviewer else "system", comment
        )
        state = self.store.get(task.run_id)
        if state is None:
            raise ReviewError(f"review task references unknown run: {task.run_id}")
        for info in state.reviews:
            if info.task_id == task_id:
                info.status = task.status
                info.decision_by = task.decision_by
                info.comment = comment
        state.pending_review_task = None
        if self.audit:
            self.audit.record(
                actor=task.decision_by or "unknown",
                action="review_decision",
                target=task.step,
                detail={"decision": decision, "comment": comment},
                request_id=task.run_id,
            )
        state.touch()
        self._persist(state)
        return self.resume(task.run_id)

    def submit_clarification(self, request_id: str, answers: Dict[str, Any]) -> RunHandle:
        state = self.store.get(request_id)
        if state is None:
            raise VafError(f"unknown run: {request_id}")
        if state.pending_clarification is None:
            raise VafError(f"run '{request_id}' is not waiting for clarification")
        state.pending_clarification.answers = dict(answers)
        state.touch()
        self._persist(state)
        return self.resume(request_id)

    def sweep_review_timeouts(self) -> List[RunHandle]:
        handles: List[RunHandle] = []
        for task, _action in self.reviews.sweep_timeouts(self.clock()):
            state = self.store.get(task.run_id)
            if state is None or state.pending_review_task != task.task_id:
                continue
            for info in state.reviews:
                if info.task_id == task.task_id:
                    info.status = "timeout"
            state.pending_review_task = None
            state.touch()
            self._persist(state)
            handles.append(self.resume(task.run_id))
        return handles

    # ==================================================================
    # 执行主循环
    # ==================================================================
    def _execute(self, state: PipelineState, reg: DomainRegistration, run_span: Span) -> RunHandle:
        pipeline = reg.pipeline
        typed = self._ensure_typed_cache(state, reg)

        while True:
            if state.status in (RunStatus.WAITING_REVIEW, RunStatus.WAITING_INPUT):
                self._persist(state)
                return RunHandle.from_state(state)

            ready, fatal = self._ready_steps(state, pipeline)
            if fatal is not None:
                raise StepFailure(fatal[0], fatal[1])
            if not ready:
                pending = [
                    s
                    for s in pipeline.steps
                    if state.step_states[s.name].status == StepStatusEnum.PENDING
                ]
                if pending:
                    for s in pending:
                        state.step_states[s.name].status = StepStatusEnum.SKIPPED
                        state.step_states[s.name].degraded_reason = "unreachable"
                    self._persist(state)
                    continue
                return self._finalize(state, reg, typed, run_span)

            # 并行组 / 串行调度
            groups: Dict[str, List[Any]] = {}
            sequential: List[Any] = []
            for step in ready:
                group = getattr(step, "parallel_group", None)
                if group:
                    groups.setdefault(group, []).append(step)
                else:
                    sequential.append(step)

            paused = False
            for group_steps in groups.values():
                if len(group_steps) == 1:
                    if self._run_step(state, reg, group_steps[0], typed, run_span):
                        paused = True
                else:
                    if self._run_parallel(state, reg, group_steps, typed, run_span):
                        paused = True
                if paused:
                    break
            if not paused:
                for step in sequential:
                    if self._run_step(state, reg, step, typed, run_span):
                        paused = True
                        break
            if paused:
                continue  # 顶部 while 检查 WAITING_* 并返回

    def _run_parallel(
        self,
        state: PipelineState,
        reg: DomainRegistration,
        steps: List[Any],
        typed: Dict[str, Any],
        run_span: Span,
    ) -> bool:
        pauses: List[bool] = [False] * len(steps)
        errors: List[Optional[Exception]] = [None] * len(steps)

        def _work(idx: int, step: Any) -> None:
            try:
                pauses[idx] = self._run_step(state, reg, step, typed, run_span)
            except Exception as exc:  # noqa: BLE001
                errors[idx] = exc

        with ThreadPoolExecutor(max_workers=len(steps), thread_name_prefix="vaf-parallel") as pool:
            futures = [pool.submit(_work, i, s) for i, s in enumerate(steps)]
            for f in futures:
                f.result()
        for exc in errors:
            if exc is not None:
                raise exc
        return any(pauses)

    # ------------------------------------------------------------------
    def _run_step(
        self,
        state: PipelineState,
        reg: DomainRegistration,
        step: Any,
        typed: Dict[str, Any],
        run_span: Span,
    ) -> bool:
        """执行单个步骤（处理器分发）；返回 True 表示流程暂停。"""
        ss = state.step_states[step.name]
        if ss.status.done:
            return False
        ss.status = StepStatusEnum.RUNNING
        ss.started_at = ss.started_at or self.clock()
        span = self._span(f"step:{step.name}", run_span, step=step.name, type=step.type)

        try:
            handler = self.handler_for(step)
            ctx = StepRunContext(self, state, reg, step, typed, ss, run_span)
            pause = handler.run(ctx)
        except StepFailure:
            ss.status = StepStatusEnum.FAILED
            span.finish("error")
            self._persist(state)
            raise
        except Exception as exc:  # noqa: BLE001
            ss.status = StepStatusEnum.FAILED
            span.finish("error", error=str(exc))
            self._persist(state)
            raise StepFailure(step.name, f"{type(exc).__name__}: {exc}") from exc

        ss.finished_at = self.clock()
        if ss.started_at:
            self._step_duration.observe(ss.finished_at - ss.started_at, type=step.type)
        span.finish("ok" if ss.status != StepStatusEnum.FAILED else "error")
        self._persist(state)
        return pause

    # ==================================================================
    # 收尾：合成 + 审核 + 终态
    # ==================================================================
    def _finalize(
        self,
        state: PipelineState,
        reg: DomainRegistration,
        typed: Dict[str, Any],
        run_span: Span,
    ) -> RunHandle:
        result: Optional[BaseModel] = None
        if reg.synthesizer is not None:
            result = reg.synthesizer(state, dict(typed))
            if not isinstance(result, BaseModel):
                raise VafError("synthesizer must return a pydantic BaseModel")

        if reg.audit_skill is not None:
            auditor = reg.skills.get(reg.audit_skill)
            if not isinstance(auditor, AuditSkill):
                raise VafError(f"audit_skill '{reg.audit_skill}' is not an AuditSkill")
            if isinstance(result, SkillOutput):
                ctx = self._build_ctx(
                    state, reg, _AuditStepRef(reg.pipeline), typed, StepState(step="__audit__")
                )
                audit_result: AuditResult = auditor.audit(result, ctx)
                if audit_result.passed:
                    pass
                elif (
                    audit_result.revision is not None
                    and reg.pipeline.on_audit_fail == "accept_revision"
                ):
                    result = audit_result.revision  # SkillOutput 实例：已过契约校验（C7）
                    self._emit("audit_revision_applied", request_id=state.request_id)
                else:
                    raise StepFailure(
                        reg.audit_skill, f"final audit rejected: {audit_result.issues[:3]}"
                    )

        if result is not None:
            state.result = result.model_dump(mode="json")
            state.result_contract = type(result).__name__
        state.status = RunStatus.SUCCEEDED
        state.error = ""
        state.touch()
        self._persist(state)
        self._run_result.inc(pipeline=state.pipeline, result="succeeded")
        self._emit("run_finished", request_id=state.request_id, status="succeeded")
        if self.audit:
            self.audit.record(
                actor="engine",
                action="run_finished",
                target=state.pipeline,
                detail={"status": "succeeded"},
                request_id=state.request_id,
            )
        run_span.finish("ok")
        with self._typed_lock:
            self._typed.pop(state.request_id, None)
            if state.request_id in self._typed_order:
                self._typed_order.remove(state.request_id)
        return RunHandle.from_state(state)

    def _fail_run(
        self,
        state: PipelineState,
        reg: DomainRegistration,
        exc: Exception,
        run_span: Span,
    ) -> RunHandle:
        message = f"{type(exc).__name__}: {exc}"
        for record in reversed(state.side_effects):  # 副作用逆序补偿
            if record.done:
                continue
            try:
                self.executor.tools.undo(record.undo_key)
                record.done = True
            except Exception:  # noqa: BLE001 - 补偿失败不掩盖主错误
                self._emit("compensation_failed", undo_key=record.undo_key)
        state.status = RunStatus.FAILED
        state.error = message
        state.touch()
        self._persist(state)
        self._run_result.inc(pipeline=state.pipeline, result="failed")
        self._emit("run_finished", request_id=state.request_id, status="failed", error=message[:200])
        if self.audit:
            self.audit.record(
                actor="engine",
                action="run_finished",
                target=state.pipeline,
                detail={"status": "failed", "error": message[:300]},
                request_id=state.request_id,
            )
        run_span.finish("error", error=message[:200])
        with self._typed_lock:
            self._typed.pop(state.request_id, None)
            if state.request_id in self._typed_order:
                self._typed_order.remove(state.request_id)
        return RunHandle.from_state(state)

    # ==================================================================
    # 共享基础设施（供 StepHandler 调用的 SPI）
    # ==================================================================
    def _require_domain(self, pipeline_name: str) -> DomainRegistration:
        reg = self._domains.get(pipeline_name)
        if reg is None:
            raise VafError(f"unknown pipeline: {pipeline_name}")
        return reg

    def _build_plan(self, reg: DomainRegistration) -> ExecutionPlan:
        return build_plan(
            pipeline_name=reg.pipeline.name,
            pipeline_version=reg.pipeline.version,
            skill_versions={name: s.version for name, s in reg.skills.items()},
            prompt_versions=self.executor.prompts.versions_snapshot(),
            config_versions=self.config.versions_snapshot(),
            model_route_digest=self.executor.router.route_digest(),
        )

    def _run_preprocessor(self, state: PipelineState, reg: DomainRegistration) -> None:
        from ..preprocessor.nodes import raw_item_from_domain_input

        if reg.pipeline.preprocessor is None or reg.preprocessor is None:
            state.variables["_preprocessed"] = []
            return
        preprocessor = reg.preprocessor()
        domain_input = reg.input_contract.model_validate(state.input)
        items = preprocessor.run([raw_item_from_domain_input(domain_input)])
        state.variables["_preprocessed"] = [i.model_dump(mode="json") for i in items]
        self._emit("preprocessed", request_id=state.request_id, items=len(items))

    def _ensure_typed_cache(self, state: PipelineState, reg: DomainRegistration) -> Dict[str, Any]:
        with self._typed_lock:
            cached = self._typed.get(state.request_id)
        if cached is not None:
            return cached
        typed: Dict[str, Any] = {}
        for step in reg.pipeline.steps:
            ss = state.step_states.get(step.name)
            if ss is None or ss.output is None:
                continue
            skill_name = getattr(step, "skill", None)
            if skill_name and skill_name in reg.skills:
                contract = reg.skills[skill_name].output_contract
                try:
                    typed[step.name] = contract.model_validate(ss.output)
                except Exception:  # noqa: BLE001 - 兼容降级/历史输出
                    typed[step.name] = ss.output
            else:
                typed[step.name] = ss.output
        with self._typed_lock:
            self._typed[state.request_id] = typed
            self._typed_order.append(state.request_id)
            self._evict_typed_locked()
        return typed

    def _build_ctx(
        self,
        state: PipelineState,
        reg: DomainRegistration,
        step: Any,
        typed: Dict[str, Any],
        ss: StepState,
    ) -> SkillContext:
        from ..preprocessor.nodes import PreprocessedItem
        from .config_view import ConfigView

        domain_input = reg.input_contract.model_validate(state.input)
        preprocessed = [
            PreprocessedItem.model_validate(p)
            for p in state.variables.get("_preprocessed", [])
        ]
        upstream: Dict[str, BaseModel] = {}
        for dep in getattr(step, "depends_on", []):
            value = typed.get(dep)
            if isinstance(value, BaseModel):
                upstream[dep] = value
        acl = getattr(step, "state_acl", None) or list(DEFAULT_STATE_ACL)
        state_view = StateView(
            acl,
            {
                "steps": {name: {"output": out} for name, out in typed.items()},
                "vars": state.variables,
            },
        )
        skill_name = getattr(step, "skill", None)
        skill = reg.skills.get(skill_name) if skill_name else None
        error_rules = self.error_memory.rules_for(skill.name, reg.name) if skill else []
        return SkillContext(
            request_id=state.request_id,
            domain=reg.name,
            domain_input=domain_input,
            data_classification=reg.data_classification,
            preprocessed=preprocessed,
            upstream_outputs=upstream,
            state_view=state_view,
            error_rules=error_rules,
            available_tools=skill.tools() if skill else [],
            loop_count=ss.loop_count,
            clarifications=dict(state.variables.get("clarifications", {})),
            config=ConfigView(self.config, state.request_id, self.tenant),
        )

    def _ready_steps(
        self, state: PipelineState, pipeline: Pipeline
    ) -> Tuple[List[Any], Optional[Tuple[str, str]]]:
        ready: List[Any] = []
        fatal: Optional[Tuple[str, str]] = None
        for step in pipeline.steps:
            ss = state.step_states[step.name]
            # PENDING：首次调度；RUNNING：暂停点（review/clarification）恢复重入
            if ss.status not in (StepStatusEnum.PENDING, StepStatusEnum.RUNNING):
                continue
            if not all(state.step_states[d].status.done for d in step.depends_on):
                failed_dep = next(
                    (
                        d
                        for d in step.depends_on
                        if state.step_states[d].status == StepStatusEnum.FAILED
                    ),
                    None,
                )
                if failed_dep and fatal is None:
                    fatal = (step.name, f"dependency '{failed_dep}' failed")
                continue
            condition = getattr(step, "condition", None)
            if condition:
                ns = self._namespace(state, self._typed.get(state.request_id, {}))
                try:
                    if not self._eval(condition, ns, f"condition:{step.name}"):
                        ss.status = StepStatusEnum.SKIPPED
                        ss.degraded_reason = "condition false"
                        continue
                except ExpressionError as exc:
                    if "missing" in str(exc).lower():
                        ss.status = StepStatusEnum.SKIPPED
                        ss.degraded_reason = f"condition missing path: {exc}"
                        self._emit("condition_missing_path", step=step.name, error=str(exc))
                        continue
                    fatal = (step.name, f"condition error: {exc}")
                    continue
            ready.append(step)
        return ready, fatal

    def _namespace(self, state: PipelineState, typed: Dict[str, Any]) -> Dict[str, Any]:
        steps_ns: Dict[str, Any] = {}
        for name, ss in state.step_states.items():
            output = typed.get(name, ss.output)
            steps_ns[name] = {
                "output": (
                    output.model_dump(mode="json") if isinstance(output, BaseModel) else output
                ),
                "status": ss.status.value,
            }
        return {
            "steps": steps_ns,
            "vars": state.variables,
            "routers": {name: {"selected": sel} for name, sel in state.routers.items()},
            "input": state.input,
        }

    def _eval(self, text: str, namespace: Dict[str, Any], context: str) -> Any:
        expr = self._compile_condition(text, context)
        return bool(expr.evaluate(namespace))

    def _check_falsification_realtime(
        self, state: PipelineState, step: str, output: BaseModel
    ) -> None:
        if not isinstance(output, SkillOutput) or not output.falsification:
            return
        context = {"input": state.input, "vars": state.variables, "step": step}
        triggered = self.falsification.run_realtime_checks(output.falsification, context)
        if triggered:
            state.falsification_flags.extend(triggered)
            self._emit(
                "falsification_triggered", request_id=state.request_id, step=step, signals=triggered
            )

    def _persist(self, state: PipelineState) -> None:
        # 合并 pending 副作用登记（工具执行线程写入）
        with self._side_effects_lock:
            pending = self._pending_side_effects.pop(state.request_id, None)
        if pending:
            known = {r.undo_key for r in state.side_effects}
            for record in pending:
                if record.undo_key not in known:
                    state.side_effects.append(record)
        state.touch()
        self.store.save(state)

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self.events is not None:
            self.events.publish(event_type, payload)

    def _span(self, name: str, parent: Optional[Span] = None, **attrs: Any) -> Span:
        if self.traces is not None:
            return self.traces.start(name, parent, **attrs)
        return Span(name=name, parent_id=parent.span_id if parent else None, attributes=attrs)


class _AuditStepRef:
    """最终审核上下文用的伪步骤（暴露全部上游输出）。"""

    def __init__(self, pipeline: Pipeline):
        self.name = "__final_audit__"
        self.skill = None
        self.depends_on = [s.name for s in pipeline.steps] if pipeline.steps else []
        self.state_acl = list(DEFAULT_STATE_ACL)
