"""输出快速构造器（DESIGN §4.5 / V2.1 P1-1）：消除证据链仪式感。

面向三类场景：确定性技能 / 审核技能修订 / 测试与黄金集构造。
**不放松任何契约校验**——产物仍是完整的 SkillOutput（引用闭合、哨兵差异化全部生效），
只是把"手写 findings+evidence 自动编号"交给框架。

用法：
    output = quick_output(
        MyOutput,
        conclusion=MyOutput.Conclusion.OK,
        confidence=Confidence.HIGH,
        claims=[("连接池耗尽是根因", "db-proxy ERROR: pool exhausted", SourceGrade.L1)],
        falsifications=[quick_falsification("池占用回落", "<80%", "推翻根因")],
    )
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple, Type, TypeVar

from ..errors import ContractError
from .base import (
    Confidence,
    Evidence,
    FalsificationCondition,
    Finding,
    SkillOutput,
    SourceGrade,
    SourceRef,
)

T = TypeVar("T", bound=SkillOutput)

# claim: (结论陈述, 证据原文[, 来源等级, 来源标签])
ClaimSpec = Tuple[str, str]


def quick_findings(
    claims: Sequence[tuple], prefix: str = ""
) -> Tuple[List[Finding], List[Evidence], List[SourceRef]]:
    """把 (claim, evidence[, grade, label]) 列表组装为引用闭合的 findings/evidence/sources。"""
    findings: List[Finding] = []
    evidence: List[Evidence] = []
    sources: List[SourceRef] = []
    seen_sources: dict = {}
    for idx, spec in enumerate(claims, start=1):
        if len(spec) < 2:
            raise ContractError(
                f"claim #{idx} must be (claim_text, evidence_text[, grade, label])"
            )
        claim_text, evidence_text = spec[0], spec[1]
        grade = spec[2] if len(spec) > 2 else SourceGrade.L2
        label = spec[3] if len(spec) > 3 else f"src{idx}"
        ev_id, finding_id = f"{prefix}ev{idx}", f"{prefix}f{idx}"
        if label in seen_sources:
            ref_id = seen_sources[label]
        else:
            ref_id = f"{prefix}s{len(seen_sources) + 1}"
            seen_sources[label] = ref_id
            sources.append(SourceRef(ref_id=ref_id, grade=grade, label=label))
        evidence.append(Evidence(ev_id=ev_id, content=evidence_text, source_ref=ref_id))
        findings.append(
            Finding(finding_id=finding_id, claim=claim_text, evidence_refs=[ev_id])
        )
    return findings, evidence, sources


def quick_falsification(
    signal: str, threshold: str, consequence: str, **extra
) -> FalsificationCondition:
    return FalsificationCondition(
        signal=signal, threshold=threshold, consequence=consequence, **extra
    )


def quick_insufficient(
    output_cls: Type[T],
    questions: Sequence[str],
    confidence: Confidence = Confidence.LOW,
    reason: str = "关键信息缺失",
    **extra,
) -> T:
    """哨兵输出快速构造：clarifying_questions 必填。"""
    return output_cls(
        conclusion="insufficient_data",  # type: ignore[arg-type]
        confidence=confidence,
        data_gaps=[
            {"item": reason, "reason": "not_disclosed", "impact": "significant"}
        ],
        clarifying_questions=list(questions),
        **extra,
    )


def quick_output(
    output_cls: Type[T],
    *,
    conclusion,
    confidence: Confidence,
    claims: Sequence[tuple],
    falsifications: Optional[Sequence[FalsificationCondition]] = None,
    assumptions: Optional[List[str]] = None,
    data_gaps: Optional[List] = None,
    **extra,
) -> T:
    """完整输出的快速构造：claims 自动组装证据链；证伪缺省给一条占位（建议显式提供）。"""
    if not claims:
        raise ContractError("quick_output requires at least one claim for non-sentinel output")
    findings, evidence, sources = quick_findings(claims)
    fals = list(falsifications) if falsifications else [
        quick_falsification("结论前提被推翻", "任一依据不成立", f"推翻 {conclusion!r} 判断")
    ]
    return output_cls(
        conclusion=conclusion,
        confidence=confidence,
        findings=findings,
        evidence=evidence,
        sources=sources,
        falsification=fals,
        assumptions=assumptions or [],
        data_gaps=data_gaps or [],
        **extra,
    )
