"""证伪引擎与处置服务测试（A5 / checker fail-fast）。"""
import pytest

from vaf.contract.base import FalsificationCondition
from vaf.contract.falsification import (
    DispositionService,
    FalsificationChecker,
    FalsificationEngine,
)
from vaf.errors import ContractError
from vaf.observe.metrics import default_metrics
from vaf.security.audit import AuditLog, MemoryAuditSink


class FeverChecker(FalsificationChecker):
    checker_id = "fever"

    def __init__(self, trigger=False):
        self.trigger = trigger

    def check(self, context):
        return self.trigger


def test_checker_requires_id():
    with pytest.raises(ContractError, match="checker_id"):

        class NoId(FalsificationChecker):
            def check(self, context):
                return False


def test_duplicate_registration_rejected():
    engine = FalsificationEngine()
    engine.register(FeverChecker())
    with pytest.raises(ContractError, match="duplicate"):
        engine.register(FeverChecker())


def test_validate_references_fail_fast_on_unknown_checker():
    engine = FalsificationEngine()
    cond = FalsificationCondition(
        signal="s", threshold="t", consequence="c", checker_id="typo_id"
    )
    with pytest.raises(ContractError, match="unregistered"):
        engine.validate_references([cond])


def test_realtime_checks_trigger_and_metrics():
    engine = FalsificationEngine()
    engine.register(FeverChecker(trigger=True))
    cond = FalsificationCondition(
        signal="体温回落", threshold="<38.5", consequence="推翻", checker_id="fever"
    )
    triggered = engine.run_realtime_checks([cond], {})
    assert triggered == ["体温回落"]
    snapshot = default_metrics().snapshot()
    assert any(
        c["labels"].get("checker") == "fever"
        for c in snapshot["counters"]["falsification_triggered_total"]
    )


def test_non_realtime_frequency_skipped():
    engine = FalsificationEngine()
    engine.register(FeverChecker(trigger=True))
    cond = FalsificationCondition(
        signal="s", threshold="t", consequence="c",
        check_frequency="daily", checker_id="fever",
    )
    assert engine.run_realtime_checks([cond], {}) == []


def test_scheduled_batch_returns_events():
    engine = FalsificationEngine()
    engine.register(FeverChecker(trigger=True))
    cond = FalsificationCondition(
        signal="s", threshold="t", consequence="c", checker_id="fever"
    )
    events = engine.run_scheduled_checks(
        [{"run_id": "r1", "step": "dx", "conditions": [cond], "context": {}}]
    )
    assert len(events) == 1 and events[0].run_id == "r1"


def test_disposition_process_full_loop():
    sink = MemoryAuditSink()
    service = DispositionService(AuditLog([sink]), notifier=lambda e, d: None)
    from vaf.contract.falsification import FalsificationEvent

    event = FalsificationEvent(
        signal="体温回落", consequence="推翻发热判断", run_id="r1", step="dx", occurred_at=0.0
    )
    outcome = service.process(event, downstream_runs=["r2", "r3"])
    assert outcome.invalidated_runs == ["r1", "r2", "r3"]
    assert outcome.recommended_reruns == ["rerun:r1", "rerun:r2", "rerun:r3"]
    assert outcome.notified
    assert service.is_invalidated("r1")
    actions = [r.action for r in sink.records("conclusion_invalidated")]
    assert actions.count("conclusion_invalidated") == 3
