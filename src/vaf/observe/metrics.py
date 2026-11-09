"""指标体系：Counter / Gauge / Histogram 与注册表。

SLA 口径说明（对应 DESIGN §12）：同步接口与异步 run 的时长指标分桶统计，
step_duration 按 skill 类型打 label，避免 V1.1 中"单技能 2s"与"P95 5s"的口径混用。
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, Dict, List, Optional


class Counter:
    def __init__(self, name: str, help_text: str = ""):
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._values: Dict[str, float] = {}

    def inc(self, value: float = 1.0, **labels: object) -> None:
        key = _label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def value(self, **labels: object) -> float:
        """按给定标签（可为部分标签）过滤求和——与 Prometheus 选择器语义一致。"""
        want = _parse_key(_label_key(labels))
        with self._lock:
            return sum(
                v for k, v in self._values.items() if _matches(_parse_key(k), want)
            )

    def snapshot(self) -> List[Dict[str, object]]:
        with self._lock:
            return [
                {"labels": _parse_key(k), "value": v}
                for k, v in sorted(self._values.items())
            ]


class Gauge:
    def __init__(self, name: str, help_text: str = ""):
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._values: Dict[str, float] = {}

    def set(self, value: float, **labels: object) -> None:
        with self._lock:
            self._values[_label_key(labels)] = value

    def value(self, **labels: object) -> float:
        want = _parse_key(_label_key(labels))
        with self._lock:
            matches = [v for k, v in self._values.items() if _matches(_parse_key(k), want)]
        return matches[-1] if matches else 0.0

    def snapshot(self) -> List[Dict[str, object]]:
        with self._lock:
            return [
                {"labels": _parse_key(k), "value": v}
                for k, v in sorted(self._values.items())
            ]


class Histogram:
    """轻量直方图：保留全部观测值，计算 count/mean/p50/p95/p99。"""

    def __init__(self, name: str, help_text: str = ""):
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._values: Dict[str, List[float]] = {}

    def observe(self, value: float, **labels: object) -> None:
        key = _label_key(labels)
        with self._lock:
            self._values.setdefault(key, []).append(value)

    def stats(self, **labels: object) -> Dict[str, float]:
        key = _label_key(labels)
        with self._lock:
            vals = sorted(self._values.get(key, []))
        if not vals:
            return {"count": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
        return {
            "count": float(len(vals)),
            "mean": sum(vals) / len(vals),
            "p50": _percentile(vals, 0.50),
            "p95": _percentile(vals, 0.95),
            "p99": _percentile(vals, 0.99),
        }

    def snapshot(self) -> List[Dict[str, object]]:
        with self._lock:
            keys = list(self._values.keys())
        return [{"labels": _parse_key(k), **self.stats(**_parse_key(k))} for k in sorted(keys)]


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, Counter] = {}
        self._gauges: Dict[str, Gauge] = {}
        self._histograms: Dict[str, Histogram] = {}

    def counter(self, name: str, help_text: str = "") -> Counter:
        with self._lock:
            if name not in self._counters:
                self._counters[name] = Counter(name, help_text)
            return self._counters[name]

    def gauge(self, name: str, help_text: str = "") -> Gauge:
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = Gauge(name, help_text)
            return self._gauges[name]

    def histogram(self, name: str, help_text: str = "") -> Histogram:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = Histogram(name, help_text)
            return self._histograms[name]

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "counters": {n: c.snapshot() for n, c in sorted(self._counters.items())},
                "gauges": {n: g.snapshot() for n, g in sorted(self._gauges.items())},
                "histograms": {n: h.snapshot() for n, h in sorted(self._histograms.items())},
            }


def _label_key(labels: Dict[str, object]) -> str:
    if not labels:
        return ""
    return ",".join(f"{k}={_flat(v)}" for k, v in sorted(labels.items()))


def _flat(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _parse_key(key: str) -> Dict[str, str]:
    if not key:
        return {}
    out: Dict[str, str] = {}
    for part in key.split(","):
        k, _, v = part.partition("=")
        out[k] = v
    return out


def _matches(have: Dict[str, str], want: Dict[str, str]) -> bool:
    return all(have.get(k) == v for k, v in want.items())


def _percentile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(q * len(sorted_vals)) - 1))
    return sorted_vals[idx]


_default_registry: Optional[MetricsRegistry] = None


def default_metrics() -> MetricsRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = MetricsRegistry()
    return _default_registry


class Timer:
    """histogram 计时上下文管理器。"""

    def __init__(self, hist: Histogram, **labels: object):
        self._hist = hist
        self._labels = labels
        self._start = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc: object) -> None:
        self._hist.observe(time.monotonic() - self._start, **self._labels)


Clock = Callable[[], float]
default_clock: Clock = time.time


def to_prometheus(registry: "MetricsRegistry") -> str:
    """注册表 → Prometheus 文本暴露格式（计数器/仪表/直方图汇总）。"""
    lines: List[str] = []
    snapshot = registry.snapshot()

    def _emit(name: str, labels: Dict[str, str], value: float, kind: str) -> None:
        label_str = ""
        if labels:
            joined = ",".join(f'{k}="{str(v).replace(chr(34), chr(39))}"' for k, v in sorted(labels.items()))
            label_str = "{" + joined + "}"
        metric = f"vaf_{name.replace('-', '_')}"
        lines.append(f"# TYPE {metric} {kind}")
        lines.append(f"{metric}{label_str} {value}")

    for _name, entries in snapshot["counters"].items():
        for entry in entries:
            _emit(_name, entry["labels"], entry["value"], "counter")
    for _name, entries in snapshot["gauges"].items():
        for entry in entries:
            _emit(_name, entry["labels"], entry["value"], "gauge")
    for name, entries in snapshot["histograms"].items():
        for entry in entries:
            labels = dict(entry["labels"])
            for stat in ("count", "mean", "p50", "p95", "p99"):
                labels["_stat"] = stat
                _emit(name, labels, entry[stat], "gauge")
    return "\n".join(lines) + "\n"
