"""人工审核队列（DESIGN §7.9 / B6）：任务生命周期 + 超时结算。"""
from __future__ import annotations

import threading
import uuid
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from ..errors import ReviewError
from ..observe.metrics import Clock, default_metrics


class ReviewTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    run_id: str
    step: str
    pipeline: str
    review_fields: List[str] = Field(default_factory=list)
    payload_digest: Dict[str, object] = Field(default_factory=dict)
    required_role: Optional[str] = None
    on_timeout: str  # pass | reject | degrade（由步骤配置带入）
    timeout_seconds: int
    status: str = "pending"  # pending | approved | rejected | timeout
    created_at: float = 0.0
    deadline: float = 0.0
    decision_by: str = ""
    comment: str = ""


class ReviewQueue:
    def __init__(self, clock: Clock):
        self._clock = clock
        self._lock = threading.Lock()
        self._tasks: Dict[str, ReviewTask] = {}
        self._metrics = default_metrics()
        self._timeouts = self._metrics.counter("review_timeouts_total", "审核超时数")

    # ------------------------------------------------------------------
    def submit(
        self,
        run_id: str,
        step: str,
        pipeline: str,
        *,
        review_fields: Optional[List[str]] = None,
        payload_digest: Optional[Dict[str, object]] = None,
        required_role: Optional[str] = None,
        on_timeout: str = "reject",
        timeout_seconds: int = 3600,
    ) -> ReviewTask:
        now = self._clock()
        task = ReviewTask(
            run_id=run_id,
            step=step,
            pipeline=pipeline,
            review_fields=list(review_fields or []),
            payload_digest=dict(payload_digest or {}),
            required_role=required_role,
            on_timeout=on_timeout,
            timeout_seconds=timeout_seconds,
            created_at=now,
            deadline=now + timeout_seconds,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Optional[ReviewTask]:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.model_copy(deep=True) if task else None

    def pending(self, pipeline: Optional[str] = None) -> List[ReviewTask]:
        with self._lock:
            tasks = [t.model_copy(deep=True) for t in self._tasks.values() if t.status == "pending"]
        if pipeline:
            tasks = [t for t in tasks if t.pipeline == pipeline]
        return sorted(tasks, key=lambda t: t.created_at)

    # ------------------------------------------------------------------
    def decide(
        self,
        task_id: str,
        decision: str,
        reviewer: str = "",
        comment: str = "",
    ) -> ReviewTask:
        if decision not in ("approved", "rejected"):
            raise ReviewError(f"invalid review decision: {decision}")
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ReviewError(f"unknown review task: {task_id}")
            if task.status != "pending":
                raise ReviewError(f"review task {task_id} already '{task.status}'")
            task.status = decision
            task.decision_by = reviewer
            task.comment = comment
            return task.model_copy(deep=True)

    def sweep_timeouts(self, now: Optional[float] = None) -> List[Tuple[ReviewTask, str]]:
        """结算超时任务，返回 (task, on_timeout 动作)。幂等：已结算的不再重复。"""
        now = now if now is not None else self._clock()
        out: List[Tuple[ReviewTask, str]] = []
        with self._lock:
            for task in self._tasks.values():
                if task.status == "pending" and now >= task.deadline:
                    task.status = "timeout"
                    self._timeouts.inc(action=task.on_timeout)
                    out.append((task.model_copy(deep=True), task.on_timeout))
        return out
