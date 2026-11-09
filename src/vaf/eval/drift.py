"""漂移检测（DESIGN §13）：JS 散度（输入字段缺失率/结论分布/置信度分布）+ 指标 3σ。"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field


def js_divergence(p: Sequence[float], q: Sequence[float], eps: float = 1e-9) -> float:
    """Jensen-Shannon 散度（对称，0~ln2）。输入为概率分布或原始计数。"""
    if len(p) != len(q):
        raise ValueError("distributions must have equal length")
    sp, sq = sum(p), sum(q)
    if sp <= 0 and sq <= 0:
        return 0.0
    pp = [x / sp if sp else eps for x in p]
    qq = [x / sq if sq else eps for x in q]
    m = [(a + b) / 2 for a, b in zip(pp, qq)]
    return 0.5 * _kl(pp, m, eps) + 0.5 * _kl(qq, m, eps)


def _kl(a: Sequence[float], b: Sequence[float], eps: float) -> float:
    return sum(x * math.log(x / y) for x, y in zip(a, b) if x > 0)


class DriftAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str  # input_missing_rate | output_distribution | metric_control_limit
    subject: str
    score: float
    threshold: float
    detail: str = ""


class DriftReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alerts: List[DriftAlert] = Field(default_factory=list)

    @property
    def has_alert(self) -> bool:
        return bool(self.alerts)


class DriftDetector:
    def __init__(self, js_threshold: float = 0.1, sigma: float = 3.0):
        self.js_threshold = js_threshold
        self.sigma = sigma

    # ------------------------------------------------------------------
    def input_missing_rate(
        self, field: str, baseline: Sequence[Dict[str, object]], current: Sequence[Dict[str, object]]
    ) -> DriftAlert:
        base_rate = _missing_rate(field, baseline)
        curr_rate = _missing_rate(field, current)
        score = abs(curr_rate - base_rate)
        return DriftAlert(
            kind="input_missing_rate",
            subject=field,
            score=round(score, 6),
            threshold=self.js_threshold,
            detail=f"baseline={base_rate:.3f} current={curr_rate:.3f}",
        )

    def output_distribution(
        self, subject: str, baseline: Dict[str, float], current: Dict[str, float]
    ) -> DriftAlert:
        categories = sorted(set(baseline) | set(current))
        p = [baseline.get(c, 0.0) for c in categories]
        q = [current.get(c, 0.0) for c in categories]
        score = js_divergence(p, q)
        return DriftAlert(
            kind="output_distribution",
            subject=subject,
            score=round(score, 6),
            threshold=self.js_threshold,
            detail=f"baseline={_fmt(p, categories)} current={_fmt(q, categories)}",
        )

    def metric_control_limit(self, metric: str, series: Sequence[float]) -> DriftAlert:
        """3σ 控制限：最后一个点越界即告警。"""
        if len(series) < 5:
            return DriftAlert(
                kind="metric_control_limit", subject=metric, score=0.0,
                threshold=self.sigma, detail="insufficient samples",
            )
        mean = sum(series) / len(series)
        var = sum((x - mean) ** 2 for x in series) / len(series)
        std = math.sqrt(var) or 1e-9
        last = series[-1]
        z = (last - mean) / std
        return DriftAlert(
            kind="metric_control_limit",
            subject=metric,
            score=round(abs(z), 4),
            threshold=self.sigma,
            detail=f"last={last:.4f} mean={mean:.4f} std={std:.4f} z={z:+.2f}",
        )

    # ------------------------------------------------------------------
    def detect(
        self,
        *,
        baseline_inputs: Sequence[Dict[str, object]] = (),
        current_inputs: Sequence[Dict[str, object]] = (),
        watch_fields: Sequence[str] = (),
        output_distributions: Sequence[tuple] = (),  # (subject, baseline_dict, current_dict)
        metric_series: Optional[Dict[str, Sequence[float]]] = None,
    ) -> DriftReport:
        alerts: List[DriftAlert] = []
        for field in watch_fields:
            alert = self.input_missing_rate(field, baseline_inputs, current_inputs)
            if alert.score > alert.threshold:
                alerts.append(alert)
        for subject, baseline, current in output_distributions:
            alert = self.output_distribution(subject, baseline, current)
            if alert.score > alert.threshold:
                alerts.append(alert)
        for metric, series in (metric_series or {}).items():
            alert = self.metric_control_limit(metric, series)
            if alert.score > alert.threshold:
                alerts.append(alert)
        return DriftReport(alerts=alerts)


def _missing_rate(field: str, rows: Sequence[Dict[str, object]]) -> float:
    if not rows:
        return 0.0
    missing = sum(1 for r in rows if field not in r or r[field] in (None, ""))
    return missing / len(rows)


def _fmt(values: Sequence[float], categories: Sequence[str]) -> str:
    return "{" + ", ".join(f"{c}:{v:.2f}" for c, v in zip(categories, values)) + "}"
