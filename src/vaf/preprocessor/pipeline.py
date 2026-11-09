"""L2 标准流水线（DESIGN §5.3 / §5.5）：Filter → Dedup → Enrich → Validate → Score。

C8 修订：节点抛出的任何异常都会——
1. 转为 Anomaly 附加到受影响条目（批量节点附加到全部条目）；
2. 指标 preprocessor_node_failures{node} 计数；
3. 事件上报；
4. 之后才按策略分流（abort 记录后 re-raise / skip 继续 / fallback 注入）。
任何路径都不会静默。
"""
from __future__ import annotations

from abc import ABC
from typing import Dict, List, Optional

from ..contract.base import Anomaly, AnomalyLevel
from ..observe.events import EventBus
from ..observe.metrics import MetricsRegistry, default_metrics
from .nodes import (
    BaseNode,
    DeduplicateNode,
    EnrichNode,
    FilterNode,
    PreprocessedItem,
    RawItem,
    ScorerNode,
    ValidatorNode,
)

_STRATEGY_LEVEL = {
    "abort": AnomalyLevel.FATAL,
    "skip": AnomalyLevel.WARNING,
    "fallback": AnomalyLevel.WARNING,
}


class Preprocessor(ABC):
    """领域预处理流水线基类：领域继承并装配节点列表。"""

    def __init__(
        self,
        *,
        metrics: Optional[MetricsRegistry] = None,
        events: Optional[EventBus] = None,
    ):
        self.filters: List[FilterNode] = []
        self.deduplicators: List[DeduplicateNode] = []
        self.enrichers: List[EnrichNode] = []
        self.validators: List[ValidatorNode] = []
        self.scorer: Optional[ScorerNode] = None
        self._metrics = metrics or default_metrics()
        self._fail_counter = self._metrics.counter(
            "preprocessor_node_failures", "预处理节点失败次数"
        )
        self._events = events

    # ------------------------------------------------------------------
    def run(self, items: List[RawItem]) -> List[PreprocessedItem]:
        items = list(items)

        # 1. 硬过滤
        for node in self.filters:
            items = self._run_filter(node, items)

        # 2. 去重
        for node in self.deduplicators:
            items = self._run_dedup(node, items)

        # 3. 数据增强
        enriched_map: Dict[str, Dict] = {item.id: {} for item in items}
        anomaly_map: Dict[str, List[Anomaly]] = {item.id: [] for item in items}
        for node in self.enrichers:
            for item in items:
                self._run_enrich(node, item, enriched_map, anomaly_map)

        # 4. 一致性校验
        for node in self.validators:
            for item in items:
                self._run_validate(node, item, anomaly_map)

        # 5. 打分
        score_map: Dict[str, float] = {}
        if self.scorer is not None:
            score_map = self._run_score(self.scorer, items, anomaly_map)

        # 6. 组装（按分数降序）
        result = [
            PreprocessedItem(
                item=item,
                score=score_map.get(item.id, 0.0),
                anomalies=anomaly_map.get(item.id, []),
                available_fields=sorted(set(item.payload.keys()) | set(enriched_map[item.id].keys())),
                enriched_data=enriched_map[item.id],
            )
            for item in items
        ]
        result.sort(key=lambda x: x.score, reverse=True)
        return result

    # ------------------------------------------------------------------
    def _fail(self, node: BaseNode, exc: Exception, detail: str) -> Anomaly:
        """统一异常捕获出口：Anomaly + 指标 + 事件，绝不静默（C8）。"""
        self._fail_counter.inc(node=node.name, strategy=node.fail_strategy)
        if self._events is not None:
            self._events.publish(
                "preprocessor_node_failed",
                {"node": node.name, "strategy": node.fail_strategy, "error": str(exc)},
            )
        return Anomaly(
            source=f"preprocessor:{node.name}",
            level=_STRATEGY_LEVEL.get(node.fail_strategy, AnomalyLevel.WARNING),
            detail=detail,
        )

    def _run_filter(self, node: FilterNode, items: List[RawItem]) -> List[RawItem]:
        try:
            return list(node.filter(items))
        except Exception as exc:  # noqa: BLE001 - 统一捕获出口
            self._fail(node, exc, f"filter failed: {exc}")
            if node.fail_strategy == "abort":
                raise
            return items  # skip / fallback：不过滤

    def _run_dedup(self, node: DeduplicateNode, items: List[RawItem]) -> List[RawItem]:
        try:
            return list(node.deduplicate(items))
        except Exception as exc:  # noqa: BLE001
            self._fail(node, exc, f"deduplicate failed: {exc}")
            if node.fail_strategy == "abort":
                raise
            return items

    def _run_enrich(
        self,
        node: EnrichNode,
        item: RawItem,
        enriched_map: Dict[str, Dict],
        anomaly_map: Dict[str, List[Anomaly]],
    ) -> None:
        try:
            enriched_map[item.id].update(node.enrich(item) or {})
        except Exception as exc:  # noqa: BLE001
            anomaly = self._fail(node, exc, f"enrich failed on item {item.id}: {exc}")
            anomaly_map[item.id].append(anomaly)
            if node.fail_strategy == "abort":
                raise
            if node.fail_strategy == "fallback" and node.fallback_value is not None:
                enriched_map[item.id].update(node.fallback_value or {})

    def _run_validate(
        self,
        node: ValidatorNode,
        item: RawItem,
        anomaly_map: Dict[str, List[Anomaly]],
    ) -> None:
        try:
            anomalies = node.validate(item) or []
            anomaly_map[item.id].extend(anomalies)
        except Exception as exc:  # noqa: BLE001
            anomaly = self._fail(node, exc, f"validate failed on item {item.id}: {exc}")
            anomaly_map[item.id].append(anomaly)
            if node.fail_strategy == "abort":
                raise

    def _run_score(
        self,
        node: ScorerNode,
        items: List[RawItem],
        anomaly_map: Dict[str, List[Anomaly]],
    ) -> Dict[str, float]:
        try:
            scored = node.score(items) or []
            return {item_id: float(score) for item_id, score in scored}
        except Exception as exc:  # noqa: BLE001
            anomaly = self._fail(node, exc, f"score failed: {exc}")
            for item in items:
                anomaly_map[item.id].append(anomaly)
            if node.fail_strategy == "abort":
                raise
            if node.fail_strategy == "fallback":
                return {item.id: float(node.fallback_value or 0.0) for item in items}
            return {}
