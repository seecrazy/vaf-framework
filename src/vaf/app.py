"""VafApp 门面（DESIGN §17）：组件组装 + 领域注册 + 对外 API。"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Type, Union

from .config.center import ConfigCenter
from .contract.base import DomainInput, SkillOutput, ensure_insufficient_member, is_insufficient
from .contract.falsification import (
    DispositionService,
    FalsificationChecker,
    FalsificationEngine,
)
from .errors import VafError
from .llm.client import LLMClient
from .llm.router import ModelRouter
from .observe.events import EventBus
from .observe.metrics import MetricsRegistry
from .observe.trace import TraceCollector
from .runtime.engine import DomainRegistration, PipelineEngine, RunHandle, SynthesizerFn
from .runtime.persistence import InMemoryStateStore, SqliteStateStore, StateStore
from .runtime.review import ReviewQueue
from .runtime.steps import Pipeline
from .security.audit import AuditLog, AuditSink, MemoryAuditSink
from .security.permissions import PermissionChecker, User
from .skill.base import AnalysisSkill, BaseSkill
from .skill.executor import SkillExecutor
from .skill.memory import ErrorMemory
from .tools.adapter import ToolAdapter
from .tools.registry import ToolRegistry, ToolSpec
import time


class VafApp:
    def __init__(
        self,
        *,
        env: str = "test",
        state_store: Optional[StateStore] = None,
        sqlite_path: Optional[str] = None,
        audit_sinks: Optional[List[AuditSink]] = None,
        metrics: Optional[MetricsRegistry] = None,
        events: Optional[EventBus] = None,
        traces: Optional[TraceCollector] = None,
        notification_hub=None,
        clock: Callable[[], float] = time.time,
    ):
        self.metrics = metrics or MetricsRegistry()
        self.events = events or EventBus()
        self.traces = traces or TraceCollector()
        self.clock = clock
        self.audit = AuditLog(audit_sinks or [MemoryAuditSink()])
        self.memory_audit = self.audit._sinks[0] if audit_sinks is None else None

        self.state_store = state_store or (
            SqliteStateStore(sqlite_path) if sqlite_path else InMemoryStateStore()
        )
        self.config = ConfigCenter(env=env, audit=self.audit)
        self.falsification = FalsificationEngine()
        self.error_memory = ErrorMemory()
        self.tool_registry = ToolRegistry()
        self.permissions = PermissionChecker()
        self.review_queue = ReviewQueue(clock)
        self.model_router = ModelRouter(clock=clock)
        self.llm: Optional[LLMClient] = None
        # 通知中心（V2.1 P2-2）：审核/澄清/证伪事件外推
        from .runtime.notification import NotificationHub

        self.notifications = notification_hub or NotificationHub()
        self.notifications.attach(self.events)

        from .skill.prompt import PromptManager

        self.prompt_manager = PromptManager()
        self.tool_adapter = ToolAdapter(
            self.tool_registry,
            permission_checker=self.permissions,
            audit=self.audit,
            metrics=self.metrics,
        )
        self.executor = SkillExecutor(
            router=self.model_router,
            prompt_manager=self.prompt_manager,
            tool_adapter=self.tool_adapter,
            falsification=self.falsification,
            error_memory=self.error_memory,
            metrics=self.metrics,
            traces=self.traces,
        )
        self.engine = PipelineEngine(
            state_store=self.state_store,
            review_queue=self.review_queue,
            executor=self.executor,
            falsification=self.falsification,
            error_memory=self.error_memory,
            config_center=self.config,
            events=self.events,
            metrics=self.metrics,
            traces=self.traces,
            audit=self.audit,
            permissions=self.permissions,
            clock=clock,
        )
        self.disposition = DispositionService(audit=self.audit)
        self._domains: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register_model_endpoint(
        self,
        name: str,
        client: LLMClient,
        *,
        tier: int = 1,
        allowed_classifications: Optional[set] = None,
    ) -> None:
        self.model_router.register(name, client, tier=tier, allowed_classifications=allowed_classifications)

    def register_tool(
        self,
        fn: Callable[[Dict[str, Any]], Dict[str, Any]],
        spec: ToolSpec,
        undo_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        self.tool_registry.register(fn, spec, undo_fn=undo_fn)

    def register_checker(self, checker: FalsificationChecker) -> None:
        self.falsification.register(checker)

    def register_domain(
        self,
        *,
        name: str,
        input_contract: Type[DomainInput],
        pipeline: Pipeline,
        skills: List[BaseSkill],
        preprocessor: Optional[Callable[[], Any]] = None,
        synthesizer: Optional[SynthesizerFn] = None,
        audit_skill: Optional[str] = None,
        data_classification: str = "internal",
    ) -> None:
        # 哨兵 fail-fast（C4）：AnalysisSkill 输出契约的结论枚举必须含哨兵值
        for skill in skills:
            if not skill.name:
                raise VafError(
                    f"skill of type {type(skill).__name__} must define a non-empty name"
                )
            if isinstance(skill, AnalysisSkill) and issubclass(skill.output_contract, SkillOutput):
                ensure_insufficient_member(skill.output_contract)  # type: ignore[arg-type]
        # pipeline 声明的 audit_skill 必须存在
        if pipeline.audit_skill and pipeline.audit_skill not in {s.name for s in skills}:
            raise VafError(
                f"pipeline '{pipeline.name}' references unknown audit_skill "
                f"'{pipeline.audit_skill}'"
            )
        reg = DomainRegistration(
            name=name,
            input_contract=input_contract,
            pipeline=pipeline,
            skills={s.name: s for s in skills},
            preprocessor=preprocessor,
            synthesizer=synthesizer,
            audit_skill=audit_skill or pipeline.audit_skill,
            data_classification=data_classification,
        )
        self.engine.register_domain(reg)
        self._domains[name] = {"registration": reg}

    # ------------------------------------------------------------------
    # 运行与交互
    # ------------------------------------------------------------------
    def run(
        self,
        pipeline_name: str,
        domain_input: Union[DomainInput, Dict[str, Any]],
        request_id: str,
        user: Optional[User] = None,
    ) -> RunHandle:
        reg = self.engine.domain_of(pipeline_name)
        if reg is None:
            raise VafError(f"unknown pipeline: {pipeline_name}")
        if not isinstance(domain_input, DomainInput):
            domain_input = reg.input_contract.model_validate(domain_input)
        if user is not None:
            from .security.permissions import Permission

            self.permissions.require(user, Permission.RUN_PIPELINE, context=pipeline_name)
        self.audit.record(
            actor=user.user_id if user else "anonymous",
            action="run_submitted",
            target=pipeline_name,
            request_id=request_id,
        )
        return self.engine.run(pipeline_name, domain_input, request_id, user=user)

    def resume(self, request_id: str, migrate: bool = False) -> RunHandle:
        return self.engine.resume(request_id, migrate=migrate)

    def submit_review(
        self,
        task_id: str,
        decision: str,
        reviewer: Optional[User] = None,
        comment: str = "",
    ) -> RunHandle:
        return self.engine.submit_review(task_id, decision, reviewer=reviewer, comment=comment)

    def submit_clarification(self, request_id: str, answers: Dict[str, Any]) -> RunHandle:
        self.audit.record(
            actor="user", action="clarification_submitted", target=request_id,
            detail={"keys": sorted(answers.keys())}, request_id=request_id,
        )
        return self.engine.submit_clarification(request_id, answers)

    def list_pending_reviews(self, pipeline: Optional[str] = None) -> List[Dict[str, Any]]:
        return [t.model_dump() for t in self.review_queue.pending(pipeline)]

    def sweep_review_timeouts(self) -> List[RunHandle]:
        return self.engine.sweep_review_timeouts()

    def get_run(self, request_id: str) -> Optional[RunHandle]:
        from .runtime.state import RunStatus

        state = self.state_store.get(request_id)
        return RunHandle.from_state(state) if state else None

    # ------------------------------------------------------------------
    # 证伪回检与处置（A5）
    # ------------------------------------------------------------------
    def run_scheduled_falsification(self, batch: List[Dict[str, Any]]):
        events = self.falsification.run_scheduled_checks(batch)
        outcomes = [self.disposition.process(e) for e in events]
        return events, outcomes

    # ------------------------------------------------------------------
    # 观测导出
    # ------------------------------------------------------------------
    def metrics_snapshot(self) -> Dict[str, Any]:
        return self.metrics.snapshot()

    def export_traces(self) -> List[Dict[str, Any]]:
        return self.traces.export()

    def inspect_run(self, request_id: str) -> Optional[Dict[str, Any]]:
        """run 排障视图（V2.1 P0-2）：状态/步骤/路由/暂停点/副作用/事件一屏汇总。"""
        state = self.state_store.get(request_id)
        if state is None:
            return None
        steps = [
            {
                "step": name,
                "status": ss.status.value,
                "attempts": ss.attempts,
                "loops": ss.loop_count,
                "contract": ss.output_contract,
                "degraded": ss.degraded_reason,
                "error": ss.error,
            }
            for name, ss in state.step_states.items()
        ]
        return {
            "request_id": state.request_id,
            "pipeline": state.pipeline,
            "domain": state.domain,
            "status": state.status.value,
            "plan_hash": state.plan_hash,
            "error": state.error,
            "steps": steps,
            "routers": state.routers,
            "pending_review_task": state.pending_review_task,
            "pending_clarification": (
                state.pending_clarification.model_dump(mode="json")
                if state.pending_clarification
                else None
            ),
            "falsification_flags": state.falsification_flags,
            "side_effects": [se.model_dump(mode="json") for se in state.side_effects],
            "clarification_rounds": len(state.clarifications),
            "result_contract": state.result_contract,
            "events_tail": self.events.tail(15),
            "traces_tail": self.traces.export()[-10:],
        }

