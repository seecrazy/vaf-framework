"""链路追踪：span 树（run → step → skill → llm/tool）。"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Span:
    name: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent_id: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    status: str = "active"  # active | ok | error
    start_ts: float = 0.0
    end_ts: Optional[float] = None

    def finish(self, status: str = "ok", **extra: Any) -> None:
        import time

        self.status = status
        self.end_ts = time.time()
        self.attributes.update(extra)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "status": self.status,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_s": None if self.end_ts is None else round(self.end_ts - self.start_ts, 6),
            "attributes": self.attributes,
        }


class TraceCollector:
    """线程安全 span 收集器。层级关系由调用方显式传递 parent_id
    （parallel_group 使用线程池，contextvar 不保证传播，故不隐式继承）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: List[Span] = []

    def start(self, name: str, parent: Optional[Span] = None, **attrs: Any) -> Span:
        import time

        span = Span(
            name=name,
            parent_id=parent.span_id if parent else None,
            attributes=attrs,
            start_ts=time.time(),
        )
        with self._lock:
            self._spans.append(span)
        return span

    def export(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [s.to_dict() for s in self._spans]

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()
