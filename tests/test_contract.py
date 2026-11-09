"""L1 契约层测试：哨兵差异化、引用闭合、强制字段推导、枚举校验。"""
import pytest
from pydantic import ValidationError

from vaf.contract.base import (
    INSUFFICIENT_DATA,
    Confidence,
    DataGap,
    DomainInput,
    Evidence,
    FalsificationCondition,
    Finding,
    SkillOutput,
    SourceGrade,
    SourceRef,
    conclusion_enum,
    ensure_insufficient_member,
    is_insufficient,
)
from vaf.errors import ContractError


class SimpleOutput(SkillOutput):
    pass


def _valid_output(**kw):
    base = dict(
        conclusion="ok",
        confidence=Confidence.HIGH,
        findings=[Finding(finding_id="f1", claim="依据", evidence_refs=["ev1"])],
        evidence=[Evidence(ev_id="ev1", content="数据", source_ref="s1")],
        sources=[SourceRef(ref_id="s1", grade=SourceGrade.L1)],
        falsification=[
            FalsificationCondition(
                signal="s", threshold="t", consequence="c", check_frequency="realtime"
            )
        ],
    )
    base.update(kw)
    return SimpleOutput(**base)


# ---------------------------------------------------------------- 哨兵
def test_sentinel_constant():
    assert INSUFFICIENT_DATA == "insufficient_data"
    assert is_insufficient(INSUFFICIENT_DATA)
    assert is_insufficient("insufficient_data")
    assert not is_insufficient("emergency")


def test_sentinel_allows_empty_findings_but_requires_questions():
    out = SimpleOutput(
        conclusion=INSUFFICIENT_DATA,
        confidence=Confidence.LOW,
        clarifying_questions=["疼痛持续多久？"],
    )
    assert out.clarifying_questions == ["疼痛持续多久？"]


def test_sentinel_without_questions_rejected():
    with pytest.raises(ValidationError, match="clarifying_question"):
        SimpleOutput(conclusion=INSUFFICIENT_DATA, confidence=Confidence.LOW)


def test_non_sentinel_requires_findings_and_falsification():
    with pytest.raises(ValidationError, match="finding"):
        SimpleOutput(conclusion="ok", confidence=Confidence.HIGH)
    with pytest.raises(ValidationError, match="falsification"):
        SimpleOutput(
            conclusion="ok",
            confidence=Confidence.HIGH,
            findings=[Finding(finding_id="f1", claim="x", evidence_refs=[])],
        )


def test_non_sentinel_rejects_clarifying_questions():
    with pytest.raises(ValidationError, match="clarifying_questions"):
        _valid_output(clarifying_questions=["为什么？"])


# ---------------------------------------------------------------- 引用闭合
def test_evidence_refs_must_resolve():
    with pytest.raises(ValidationError, match="unknown evidence"):
        _valid_output(
            findings=[Finding(finding_id="f1", claim="x", evidence_refs=["ev_missing"])]
        )


def test_source_refs_must_resolve():
    with pytest.raises(ValidationError, match="unknown source"):
        _valid_output(evidence=[Evidence(ev_id="ev1", content="x", source_ref="s_missing")])


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        _valid_output(
            findings=[
                Finding(finding_id="f1", claim="a", evidence_refs=["ev1"]),
                Finding(finding_id="f1", claim="b", evidence_refs=["ev1"]),
            ]
        )


# ---------------------------------------------------------------- 强制字段推导（C3）
def test_mandatory_fields_derived_from_metadata():
    fields = SimpleOutput.mandatory_fields()
    assert "conclusion" in fields
    assert "confidence" in fields
    # 默认空列表的集合字段不应误报为强制
    assert "data_gaps" not in fields
    assert "findings" not in fields


# ---------------------------------------------------------------- 枚举哨兵校验（C4）
def test_conclusion_enum_extraction():
    from enum import Enum

    class C(str, Enum):
        A = "a"
        INS = INSUFFICIENT_DATA

    class Out(SkillOutput):
        conclusion: C

    assert conclusion_enum(Out) is C
    ensure_insufficient_member(Out)  # 不抛


def test_ensure_insufficient_member_fails_without_sentinel():
    from enum import Enum

    class C(str, Enum):
        A = "a"

    class Out(SkillOutput):
        conclusion: C

    with pytest.raises(ContractError, match="sentinel"):
        ensure_insufficient_member(Out)


def test_domain_input_forbids_extra_fields():
    class Inp(DomainInput):
        extra_field: str = ""

    with pytest.raises(ValidationError, match="Extra inputs"):
        Inp(raw_id="1", source="s", timestamp="t", unexpected=1)


def test_data_gap_reason_enum():
    gap = DataGap(item="体温", reason="unavailable", impact="minor")
    assert gap.impact == "minor"
    with pytest.raises(ValidationError):
        DataGap(item="x", reason="made_up", impact="none")
