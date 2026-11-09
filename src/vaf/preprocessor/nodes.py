"""L2 预处理层原子节点（DESIGN §5.2）：五类节点 + 失败策略。

失败策略 fail_strategy ∈ abort | skip | fallback（fallback 需提供 fallback_value）。
异常如何被捕获与记录见 pipeline.py（C8：任何路径都不静默）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from ..contract.base import Anomaly, AnomalyLevel, DomainInput

NodeFailStrategy = Literal["abort", "skip", "fallback"]


class RawItem(BaseModel):
    """L2 标准条目；由 DomainInput 桥接（C9）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    source: str = ""
    timestamp: str = ""


def raw_item_from_domain_input(di: DomainInput) -> RawItem:
    return RawItem(
        id=di.raw_id,
        payload=di.model_dump(),
        source=di.source,
        timestamp=di.timestamp,
    )


class PreprocessedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: RawItem
    score: float = 0.0
    anomalies: List[Anomaly] = Field(default_factory=list)
    available_fields: List[str] = Field(default_factory=list)
    enriched_data: Dict[str, Any] = Field(default_factory=dict)

    @property
    def has_fatal(self) -> bool:
        return any(a.level == AnomalyLevel.FATAL for a in self.anomalies)


class BaseNode(ABC):
    """所有预处理节点的基类。name 必填且非空（子类定义期校验）。"""

    name: str = ""
    fail_strategy: NodeFailStrategy = "abort"
    fallback_value: Any = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # 仅校验直接定义了空 name 的具体类
        if not cls.__dict__.get("__abstractmethods__") and cls.__dict__.get("name", None) == "":
            import inspect

            if not inspect.isabstract(cls):
                raise ValueError(f"node class '{cls.__name__}' must define a non-empty name")


class FilterNode(BaseNode, ABC):
    @abstractmethod
    def filter(self, items: List[RawItem]) -> List[RawItem]:  # pragma: no cover
        raise NotImplementedError


class DeduplicateNode(BaseNode, ABC):
    @abstractmethod
    def deduplicate(self, items: List[RawItem]) -> List[RawItem]:  # pragma: no cover
        raise NotImplementedError


class EnrichNode(BaseNode, ABC):
    @abstractmethod
    def enrich(self, item: RawItem) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


class ValidatorNode(BaseNode, ABC):
    @abstractmethod
    def validate(self, item: RawItem) -> List[Anomaly]:  # pragma: no cover
        raise NotImplementedError


class ScorerNode(BaseNode, ABC):
    @abstractmethod
    def score(self, items: List[RawItem]) -> List[Tuple[str, float]]:  # pragma: no cover
        """返回 [(item_id, score)]。"""
        raise NotImplementedError
