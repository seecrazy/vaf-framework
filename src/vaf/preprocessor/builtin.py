"""框架内置跨领域预处理组件（DESIGN §5.4）。"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..contract.base import Anomaly, AnomalyLevel
from .nodes import (
    DeduplicateNode,
    EnrichNode,
    FilterNode,
    RawItem,
    ScorerNode,
    ValidatorNode,
)


class HashDeduplicateNode(DeduplicateNode):
    """按 payload 子集字段的指纹去重（字段为空则全 payload）。"""

    name = "hash_dedup"
    fail_strategy = "skip"

    def __init__(self, fields: Optional[List[str]] = None):
        self.fields = fields or []

    def _fingerprint(self, item: RawItem) -> str:
        data = item.payload if not self.fields else {k: item.payload.get(k) for k in self.fields}
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()

    def deduplicate(self, items: List[RawItem]) -> List[RawItem]:
        seen: Dict[str, RawItem] = {}
        for item in items:
            fp = self._fingerprint(item)
            if fp not in seen:
                seen[fp] = item
        return list(seen.values())


class KeywordFilterNode(FilterNode):
    """排除 payload[field] 含任一关键字的条目。"""

    name = "keyword_filter"
    fail_strategy = "skip"

    def __init__(self, field: str, keywords: Iterable[str]):
        self.field = field
        self.keywords = [k.lower() for k in keywords]

    def filter(self, items: List[RawItem]) -> List[RawItem]:
        out: List[RawItem] = []
        for item in items:
            value = str(item.payload.get(self.field, "")).lower()
            if not any(k in value for k in self.keywords):
                out.append(item)
        return out


def resolve_path(payload: Any, path: str) -> Tuple[bool, Any]:
    """按点路径取值（'vitals.heart_rate'）；返回 (found, value)。"""
    node = payload
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return False, None
    return True, node


class FieldPresenceValidator(ValidatorNode):
    """必填字段缺失检查（支持嵌套点路径）：缺失 → WARNING 异常标记。"""

    name = "field_presence"
    fail_strategy = "skip"

    def __init__(self, fields: List[str], level: AnomalyLevel = AnomalyLevel.WARNING):
        self.fields = fields
        self.level = level

    def validate(self, item: RawItem) -> List[Anomaly]:
        missing = [
            f
            for f in self.fields
            if resolve_path(item.payload, f)[1] in (None, "") or not resolve_path(item.payload, f)[0]
        ]
        return [
            Anomaly(source=f"validator:{self.name}", level=self.level, detail=f"missing field: {m}")
            for m in missing
        ]


class RangeValidatorNode(ValidatorNode):
    """数值字段区间校验（支持嵌套点路径）：越界 → 异常标记（默认 WARNING，可配置 FATAL）。"""

    name = "range_validator"
    fail_strategy = "skip"

    def __init__(
        self,
        field: str,
        low: Optional[float] = None,
        high: Optional[float] = None,
        level: AnomalyLevel = AnomalyLevel.WARNING,
    ):
        self.field = field
        self.low = low
        self.high = high
        self.level = level

    def validate(self, item: RawItem) -> List[Anomaly]:
        found, value = resolve_path(item.payload, self.field)
        if not found or value is None:
            return []
        try:
            num = float(value)
        except (TypeError, ValueError):
            return [
                Anomaly(
                    source=f"validator:{self.name}",
                    level=self.level,
                    detail=f"field {self.field} not numeric: {value!r}",
                )
            ]
        out: List[Anomaly] = []
        if self.low is not None and num < self.low:
            out.append(
                Anomaly(
                    source=f"validator:{self.name}",
                    level=self.level,
                    detail=f"field {self.field}={num} below {self.low}",
                )
            )
        if self.high is not None and num > self.high:
            out.append(
                Anomaly(
                    source=f"validator:{self.name}",
                    level=self.level,
                    detail=f"field {self.field}={num} above {self.high}",
                )
            )
        return out


_PII_PATTERNS = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone_cn", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("id_card_cn", re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")),
]


class PIIDetectionNode(ValidatorNode):
    """PII 检出（配合脱敏策略使用），检出 → WARNING 异常标记。"""

    name = "pii_detection"
    fail_strategy = "skip"

    def __init__(self, fields: Optional[List[str]] = None):
        self.fields = fields

    def validate(self, item: RawItem) -> List[Anomaly]:
        out: List[Anomaly] = []
        fields = self.fields or list(item.payload.keys())
        for f in fields:
            value = item.payload.get(f)
            if not isinstance(value, str):
                continue
            for kind, pattern in _PII_PATTERNS:
                if pattern.search(value):
                    out.append(
                        Anomaly(
                            source=f"validator:{self.name}",
                            level=AnomalyLevel.WARNING,
                            detail=f"PII ({kind}) detected in field {f}",
                        )
                    )
        return out


class LengthTruncateNode(EnrichNode):
    """长文本字段截断，结果写入 enriched_data（不污染原始 payload）。"""

    name = "length_truncate"
    fail_strategy = "skip"

    def __init__(self, field: str, max_chars: int = 2000):
        self.field = field
        self.max_chars = max_chars

    def enrich(self, item: RawItem) -> Dict[str, Any]:
        value = item.payload.get(self.field)
        if isinstance(value, str) and len(value) > self.max_chars:
            return {f"{self.field}__truncated": value[: self.max_chars] + "…[truncated]"}
        return {}


class StaticScorerNode(ScorerNode):
    """静态映射打分：payload[field] → score_map 映射，缺省 default_score。"""

    name = "static_scorer"
    fail_strategy = "fallback"
    fallback_value = 0.0

    def __init__(self, field: str, score_map: Dict[str, float], default_score: float = 0.0):
        self.field = field
        self.score_map = score_map
        self.default_score = default_score

    def score(self, items: List[RawItem]) -> List[Tuple[str, float]]:
        return [
            (item.id, float(self.score_map.get(str(item.payload.get(self.field, "")), self.default_score)))
            for item in items
        ]
