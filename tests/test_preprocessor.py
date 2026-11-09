"""L2 预处理层测试：异常全捕获矩阵（C8）+ 内置组件 + 排序。"""
import pytest

from vaf.contract.base import Anomaly, AnomalyLevel
from vaf.observe.events import EventBus
from vaf.observe.metrics import MetricsRegistry
from vaf.preprocessor.builtin import (
    FieldPresenceValidator,
    HashDeduplicateNode,
    KeywordFilterNode,
    LengthTruncateNode,
    PIIDetectionNode,
    RangeValidatorNode,
    StaticScorerNode,
)
from vaf.preprocessor.nodes import (
    EnrichNode,
    FilterNode,
    PreprocessedItem,
    RawItem,
    ScorerNode,
    ValidatorNode,
    raw_item_from_domain_input,
)
from vaf.preprocessor.pipeline import Preprocessor
from vaf.contract.base import DomainInput


class MyInput(DomainInput):
    note: str = ""


def _items(*payloads):
    return [
        RawItem(id=f"i{n}", payload=dict(p, __n=n), source="t", timestamp="0")
        for n, p in enumerate(payloads)
    ]


# ---------------------------------------------------------------- 异常捕获矩阵
class BoomFilter(FilterNode):
    name = "boom_filter"
    fail_strategy = "skip"

    def filter(self, items):
        raise RuntimeError("filter exploded")


class BoomEnrich(EnrichNode):
    name = "boom_enrich"
    fail_strategy = "fallback"
    fallback_value = {"default": True}

    def enrich(self, item):
        raise RuntimeError("enrich exploded")


class BoomAbortEnrich(EnrichNode):
    name = "boom_abort"
    fail_strategy = "abort"

    def enrich(self, item):
        raise RuntimeError("abort!")


class OkValidator(ValidatorNode):
    name = "ok_validator"
    fail_strategy = "skip"

    def validate(self, item):
        return [Anomaly(source="v", level=AnomalyLevel.WARNING, detail="w")]


class ScoreByN(ScorerNode):
    name = "score_by_n"
    fail_strategy = "fallback"
    fallback_value = 0.0

    def score(self, items):
        return [(i.id, float(i.payload.get("n", 0))) for i in items]


def _pipeline(metrics=None, events=None, **kw):
    p = Preprocessor(metrics=metrics, events=events)
    for attr, nodes in kw.items():
        if attr == "scorer":
            p.scorer = nodes
        else:
            setattr(p, attr, list(nodes))
    return p


def test_filter_failure_recorded_and_skipped():
    metrics = MetricsRegistry()
    events = EventBus()
    p = _pipeline(metrics=metrics, events=events, filters=[BoomFilter()])
    result = p.run(_items({"a": 1}))
    assert len(result) == 1  # skip：不过滤
    assert metrics.counter("preprocessor_node_failures").value(node="boom_filter") == 1
    assert events.tail()[0]["type"] == "preprocessor_node_failed"


def test_enrich_failure_fallback_value_injected():
    metrics = MetricsRegistry()
    p = _pipeline(metrics=metrics, enrichers=[BoomEnrich()])
    result = p.run(_items({"a": 1}))
    assert result[0].enriched_data == {"default": True}
    anomalies = result[0].anomalies
    assert any("boom_enrich" in a.source for a in anomalies)


def test_abort_strategy_reraises_after_recording():
    metrics = MetricsRegistry()
    p = _pipeline(metrics=metrics, enrichers=[BoomAbortEnrich()])
    with pytest.raises(RuntimeError, match="abort!"):
        p.run(_items({"a": 1}))
    # 记录发生在 re-raise 之前（C8：abort 也不静默）
    assert metrics.counter("preprocessor_node_failures").value(node="boom_abort") == 1


def test_fatal_anomaly_flag():
    p = _pipeline(validators=[OkValidator()])
    result = p.run(_items({"a": 1}))
    assert not result[0].has_fatal


def test_sorted_by_score_desc():
    p = _pipeline(scorer=ScoreByN())
    result = p.run(_items({"n": 1}, {"n": 5}, {"n": 3}))
    assert [r.item.id for r in result] == ["i1", "i2", "i0"]


def test_domain_input_bridge():
    di = MyInput(raw_id="r1", source="s", timestamp="t", note="hello")
    item = raw_item_from_domain_input(di)
    assert item.id == "r1"
    assert item.payload["note"] == "hello"


# ---------------------------------------------------------------- 内置组件
def test_hash_dedup():
    node = HashDeduplicateNode(fields=["k"])
    items = _items({"k": "a", "x": 1}, {"k": "a", "x": 2}, {"k": "b"})
    assert len(node.deduplicate(items)) == 2


def test_keyword_filter():
    node = KeywordFilterNode("title", ["closed"])
    items = _items({"title": "open ok"}, {"title": "closed incident"})
    assert len(node.filter(items)) == 1


def test_field_presence():
    node = FieldPresenceValidator(["a", "b"])
    anomalies = node.validate(RawItem(id="x", payload={"a": 1}))
    assert len(anomalies) == 1 and "b" in anomalies[0].detail


def test_range_validator():
    node = RangeValidatorNode("t", low=35, high=42)
    hot = node.validate(RawItem(id="x", payload={"t": 43.0}))
    ok = node.validate(RawItem(id="x", payload={"t": 36.5}))
    assert len(hot) == 1 and ok == []


def test_pii_detection():
    node = PIIDetectionNode(fields=["text"])
    found = node.validate(RawItem(id="x", payload={"text": "联系 13800138000"}))
    assert any("PII" in a.detail for a in found)


def test_length_truncate_writes_enriched():
    node = LengthTruncateNode("long", max_chars=5)
    out = node.enrich(RawItem(id="x", payload={"long": "abcdefgh"}))
    assert out["long__truncated"].startswith("abcde")
    assert node.enrich(RawItem(id="x", payload={"long": "ab"})) == {}


def test_static_scorer():
    node = StaticScorerNode("sev", {"critical": 100, "warning": 50}, default_score=10)
    scored = node.score(_items({"sev": "critical"}, {"sev": "unknown"}))
    assert dict(scored) == {"i0": 100.0, "i1": 10.0}
