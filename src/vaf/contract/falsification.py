"""可执行证伪：校验器注册（fail-fast）+ 引擎 + 处置服务（DESIGN §4.3 / §6.4）。

V2.0 关键修订（A5）：
- checker_id 注册期/引用期 fail-fast，杜绝 V1.1 拼错即静默跳过；
- 处置闭环：触发 → 影响面 → 通知 → 标记失效 → 建议重跑，全程审计留痕。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from ..errors import ContractError
from ..observe.metrics import default_metrics
from ..security.audit import AuditLog
from .base import FalsificationCondition, SkillOutput


class FalsificationChecker(ABC):
    """证伪校验器基类。check 返回 True 表示触发证伪（结论失效）。"""

    checker_id: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.checker_id:
            raise ContractError(
                f"FalsificationChecker subclass '{cls.__name__}' must define a non-empty checker_id"
            )

    @abstractmethod
    def check(self, context: Dict[str, object]) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class FalsificationEngine:
    """校验器注册表 + 实时/定时回检。未注册 checker_id 一律 fail-fast。"""

    def __init__(self) -> None:
        self._checkers: Dict[str, FalsificationChecker] = {}
        self._metrics = default_metrics()
        self._triggered_total = self._metrics.counter(
            "falsification_triggered_total", "证伪条件触发次数"
        )

    # -- 注册与校验 ---------------------------------------------------------
    def register(self, checker: FalsificationChecker) -> None:
        if not checker.checker_id:
            raise ContractError("checker_id must be non-empty")
        if checker.checker_id in self._checkers:
            raise ContractError(f"duplicate checker registration: {checker.checker_id}")
        self._checkers[checker.checker_id] = checker

    def has(self, checker_id: str) -> bool:
        return checker_id in self._checkers

    def validate_references(self, conditions: List[FalsificationCondition]) -> None:
        """引用校验：绑定 checker_id 的条件，其校验器必须已注册（fail-fast）。"""
        for cond in conditions:
            if cond.checker_id and cond.checker_id not in self._checkers:
                raise ContractError(
                    f"falsification condition '{cond.signal}' references unregistered "
                    f"checker '{cond.checker_id}'"
                )

    def registered_ids(self) -> List[str]:
        return sorted(self._checkers.keys())

    # -- 执行 ---------------------------------------------------------------
    def run_realtime_checks(
        self, conditions: List[FalsificationCondition], context: Dict[str, object]
    ) -> List[str]:
        """推理完成后实时校验，返回被触发的 signal 列表。"""
        self.validate_references(conditions)
        triggered: List[str] = []
        for cond in conditions:
            if cond.check_frequency != "realtime" or not cond.checker_id:
                continue
            if self._checkers[cond.checker_id].check(context):
                triggered.append(cond.signal)
                self._triggered_total.inc(checker=cond.checker_id)
        return triggered

    def run_scheduled_checks(
        self,
        batch: List[Dict[str, object]],
    ) -> List["FalsificationEvent"]:
        """定时批量回检。batch 元素：{run_id, step, conditions, context}。"""
        events: List[FalsificationEvent] = []
        for item in batch:
            conditions: List[FalsificationCondition] = list(item["conditions"])
            context: Dict[str, object] = dict(item.get("context", {}))
            self.validate_references(conditions)
            for cond in conditions:
                if not cond.checker_id:
                    continue
                if self._checkers[cond.checker_id].check(context):
                    events.append(
                        FalsificationEvent(
                            signal=cond.signal,
                            consequence=cond.consequence,
                            run_id=str(item.get("run_id", "")),
                            step=str(item.get("step", "")),
                            occurred_at=time.time(),
                        )
                    )
                    self._triggered_total.inc(checker=cond.checker_id)
        return events


class FalsificationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal: str
    consequence: str
    run_id: str
    step: str
    occurred_at: float


@dataclass
class DispositionOutcome:
    event: FalsificationEvent
    affected_runs: List[str] = field(default_factory=list)
    invalidated_runs: List[str] = field(default_factory=list)
    recommended_reruns: List[str] = field(default_factory=list)
    notified: bool = False


class DispositionService:
    """证伪处置闭环（A5）：影响面 → 通知 → 失效标记 → 建议重跑。"""

    def __init__(
        self,
        audit: AuditLog,
        notifier: Optional[Callable[[FalsificationEvent, List[str]], None]] = None,
    ):
        self._audit = audit
        self._notifier = notifier
        self._invalidated: Dict[str, FalsificationEvent] = {}

    def impact_lookup(
        self,
        event: FalsificationEvent,
        run_index: Callable[[str], List[str]],
    ) -> List[str]:
        """run_index(结论指纹) 返回引用了该 run 结论的下游 run 列表。"""
        return list(run_index(event.run_id))

    def process(
        self,
        event: FalsificationEvent,
        downstream_runs: Optional[List[str]] = None,
    ) -> DispositionOutcome:
        downstream = list(downstream_runs or [])
        self._invalidated[event.run_id] = event
        for rid in [event.run_id] + downstream:
            self._audit.record(
                actor="falsification_engine",
                action="conclusion_invalidated",
                target=rid,
                detail={"signal": event.signal, "consequence": event.consequence},
                request_id=event.run_id,
            )
        if self._notifier is not None:
            self._notifier(event, downstream)
        recommended = [f"rerun:{rid}" for rid in [event.run_id] + downstream]
        self._audit.record(
            actor="falsification_engine",
            action="rerun_recommended",
            target=event.run_id,
            detail={"recommended": recommended},
            request_id=event.run_id,
        )
        return DispositionOutcome(
            event=event,
            affected_runs=downstream,
            invalidated_runs=[event.run_id] + downstream,
            recommended_reruns=recommended,
            notified=self._notifier is not None,
        )

    def is_invalidated(self, run_id: str) -> bool:
        return run_id in self._invalidated
