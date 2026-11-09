"""模型路由器（DESIGN §8.3）：数据分级硬绑定 + 熔断 + tier 降级。

B3 修订：confidential 级数据路由到未授权端点 → PolicyViolationError（fail-closed）。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from ..errors import PolicyViolationError
from ..observe.metrics import default_metrics
from .client import LLMClient


@dataclass
class _EndpointState:
    breaker_window: deque = field(default_factory=lambda: deque(maxlen=20))  # (ts, ok)
    breaker_opened_at: Optional[float] = None
    consecutive_failures: int = 0


@dataclass
class ModelEndpoint:
    name: str
    client: LLMClient
    tier: int
    allowed_classifications: Set[str]
    state: _EndpointState = field(default_factory=_EndpointState)


class CircuitBreakerConfig:
    def __init__(
        self,
        error_rate_threshold: float = 0.5,
        min_samples: int = 5,
        open_seconds: float = 60.0,
    ):
        self.error_rate_threshold = error_rate_threshold
        self.min_samples = min_samples
        self.open_seconds = open_seconds


class ModelRouter:
    def __init__(
        self,
        breaker_config: Optional[CircuitBreakerConfig] = None,
        clock: Callable[[], float] = time.time,
    ):
        self._endpoints: Dict[str, ModelEndpoint] = {}
        self._lock = threading.Lock()
        self.breaker = breaker_config or CircuitBreakerConfig()
        self._clock = clock
        self._metrics = default_metrics()
        self._route_total = self._metrics.counter("model_route_total", "模型路由次数")
        self._rejections = self._metrics.counter("model_route_rejected", "路由策略拒绝次数")

    # ------------------------------------------------------------------
    def register(
        self,
        name: str,
        client: LLMClient,
        tier: int = 1,
        allowed_classifications: Optional[Set[str]] = None,
    ) -> None:
        allowed = set(allowed_classifications) if allowed_classifications else {
            "public", "internal", "sensitive", "confidential"
        }
        with self._lock:
            if name in self._endpoints:
                raise ValueError(f"duplicate model endpoint: {name}")
            self._endpoints[name] = ModelEndpoint(name, client, tier, allowed)

    def endpoint_names(self) -> List[str]:
        with self._lock:
            return sorted(self._endpoints.keys())

    def route_digest(self) -> str:
        with self._lock:
            parts = sorted(
                f"{e.name}:tier{e.tier}:{'+'.join(sorted(e.allowed_classifications))}"
                for e in self._endpoints.values()
            )
        return "|".join(parts)

    # ------------------------------------------------------------------
    def route(
        self,
        preferred_tier: int = 1,
        data_classification: str = "internal",
        tenant: str = "default",
        exclude: Optional[Set[str]] = None,
    ) -> ModelEndpoint:
        with self._lock:
            candidates = [
                e for e in self._endpoints.values()
                if data_classification in e.allowed_classifications
                and (exclude is None or e.name not in exclude)
            ]
        if not candidates:
            # fail-closed（B3）：没有兼容数据分级的端点，直接拒绝
            self._rejections.inc(reason="classification_no_endpoint")
            raise PolicyViolationError(
                f"no model endpoint allows data_classification='{data_classification}' "
                f"(tenant={tenant}); refusing to route to non-compliant endpoints"
            )
        # tier 距离排序 + 熔断过滤
        candidates.sort(key=lambda e: (abs(e.tier - preferred_tier), e.tier))
        for endpoint in candidates:
            if not self._is_open(endpoint):
                self._route_total.inc(endpoint=endpoint.name, classification=data_classification)
                return endpoint
        # 全部熔断：选最早进入 half-open 的探测
        fallback = candidates[0]
        self._route_total.inc(endpoint=fallback.name, classification=data_classification, half_open=True)
        return fallback

    # ------------------------------------------------------------------
    def _is_open(self, endpoint: ModelEndpoint) -> bool:
        now = self._clock()
        st = endpoint.state
        if st.breaker_opened_at is not None:
            if now - st.breaker_opened_at >= self.breaker.open_seconds:
                st.breaker_opened_at = None  # half-open：允许探测
                return False
            return True
        if self._window_trips(st):
            st.breaker_opened_at = now
            return True
        return False

    def record_success(self, endpoint_name: str) -> None:
        with self._lock:
            endpoint = self._endpoints.get(endpoint_name)
            if endpoint is None:
                return
            endpoint.state.breaker_window.append((self._clock(), True))
            endpoint.state.consecutive_failures = 0

    def record_failure(self, endpoint_name: str) -> None:
        with self._lock:
            endpoint = self._endpoints.get(endpoint_name)
            if endpoint is None:
                return
            endpoint.state.breaker_window.append((self._clock(), False))
            endpoint.state.consecutive_failures += 1

    def breaker_state(self, endpoint_name: str) -> str:
        with self._lock:
            endpoint = self._endpoints.get(endpoint_name)
            if endpoint is None:
                return "unknown"
            if endpoint.state.breaker_opened_at is not None:
                if self._clock() - endpoint.state.breaker_opened_at >= self.breaker.open_seconds:
                    endpoint.state.breaker_opened_at = None  # half-open
                    return "half_open"
                return "open"
            # 惰性求值：窗口满足条件时当场打开
            if self._window_trips(endpoint.state):
                endpoint.state.breaker_opened_at = self._clock()
                return "open"
            return "closed"

    def _window_trips(self, st: _EndpointState) -> bool:
        window = list(st.breaker_window)
        if len(window) < self.breaker.min_samples:
            return False
        errors = sum(1 for _, ok in window if not ok)
        return errors / len(window) >= self.breaker.error_rate_threshold
