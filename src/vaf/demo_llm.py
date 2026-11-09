"""内置演示用确定性 LLM 响应器（CLI demo / 离线评估）。

按系统提示词中的技能名标记（「skill_name」）路由；文本特征（如"咳嗽"）驱动
不同病例轨迹。生产环境替换为真实 Provider，本模块仅用于演示与测试。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

from .llm.client import LLMResponse, ToolCallRequest
from .llm.mock import tool_call


def _triage(conclusion: str, confidence: str, n_findings: int = 2, **extra: Any) -> str:
    data: Dict[str, Any] = {
        "conclusion": conclusion,
        "confidence": confidence,
        "findings": [
            {"finding_id": f"f{i}", "claim": f"临床依据{i}", "evidence_refs": [f"ev{i}"]}
            for i in range(n_findings)
        ],
        "evidence": [
            {"ev_id": f"ev{i}", "content": f"输入与体征数据{i}"} for i in range(n_findings)
        ],
        "falsification": [
            {
                "signal": "复测体温",
                "threshold": "<38.5",
                "consequence": "推翻发热相关判断",
                "check_frequency": "realtime",
            }
        ],
    }
    data.update(extra)
    return json.dumps(data, ensure_ascii=False)


def _ops_out(conclusion: str, confidence: str, **extra: Any) -> str:
    data: Dict[str, Any] = {
        "conclusion": conclusion,
        "confidence": confidence,
        "findings": [
            {"finding_id": "f0", "claim": "告警：db-proxy 连接池耗尽", "evidence_refs": ["ev0"]},
            {"finding_id": "f1", "claim": "告警：payment-api 上游超时", "evidence_refs": ["ev1"]},
        ],
        "evidence": [
            {"ev_id": "ev0", "content": "db-proxy connection pool exhausted"},
            {"ev_id": "ev1", "content": "payment-api upstream timeout"},
        ],
        "falsification": [
            {
                "signal": "db-proxy 连接池指标回落",
                "threshold": "active < 80% max",
                "consequence": "推翻连接池耗尽根因",
                "check_frequency": "realtime",
            }
        ],
    }
    data.update(extra)
    return json.dumps(data, ensure_ascii=False)


class DemoResponder:
    """两个示例领域的全技能确定性响应。"""

    def __init__(self) -> None:
        self._probe_round = 0

    def __call__(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        response_schema: Optional[Dict[str, Any]],
        **opts: Any,
    ) -> Union[str, LLMResponse, List[ToolCallRequest]]:
        system = messages[0]["content"] if messages else ""
        mild = ("咳嗽" in system) or ("cough" in system)

        # ---------------- medical_triage ----------------
        if "「symptom_intake」" in system:
            if mild:
                return json.dumps(
                    {"symptoms": ["cough"], "duration_hours": 48, "severity_1to10": 2},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"symptoms": ["chest_pain"], "duration_hours": 3, "severity_1to10": 7},
                ensure_ascii=False,
            )
        if "「vitals_risk_check」" in system:
            if mild:
                return _triage("routine", "high", 1)
            return _triage("urgent", "medium", 1)
        if "「triage_clarify」" in system:
            if mild:
                return _triage("routine", "high", 1)
            if "已收集澄清回答：{}" in system:
                return json.dumps(
                    {
                        "conclusion": "insufficient_data",
                        "confidence": "low",
                        "clarifying_questions": ["疼痛是否放射至左臂或下颌？", "是否伴随出汗与恶心？"],
                    },
                    ensure_ascii=False,
                )
            return _triage("emergency", "high", 2)
        if "「differential_diagnosis」" in system:
            if mild:
                return _triage("routine", "high", 1, recommended_department="全科")
            return _triage(
                "emergency",
                "high",
                2,
                red_flags=["radiating_pain", "diaphoresis"],
                recommended_department="急诊科",
                suggested_actions=["立即开通胸痛绿色通道", "10 分钟内完成心电图"],
            )
        if "「risk_stratification」" in system:
            return _triage("emergency", "high", 1, suggested_actions=["监护转运", "建立静脉通路"])

        # ---------------- ops_incident ----------------
        if "「alert_triage」" in system:
            return _ops_out("critical", "high", suspected_components=["db-proxy"])
        if "「hypothesis_gen」" in system:
            return _ops_out(
                "degraded",
                "high",
                hypotheses=[{"component": "db-proxy", "guess": "连接池耗尽", "confidence": "high"}],
                next_probe_target="db-proxy",
            )
        if "「probe_executor」" in system:
            has_tool_result = any(m.get("role") == "tool" for m in messages)
            if not has_tool_result:
                return [tool_call("log_probe", {"target": "db-proxy", "pattern": "error"})]
            gaps = (
                []
                if self._probe_round >= 1
                else [{"item": "payment-api 侧日志", "reason": "unavailable", "impact": "minor"}]
            )
            self._probe_round += 1
            return _ops_out(
                "degraded",
                "high",
                probe_target="db-proxy",
                evidence_found=True,
                probe_summary="db-proxy ERROR: connection pool exhausted",
                data_gaps=gaps,
            )
        if "「prepare_report_input」" in system:
            return json.dumps(
                {
                    "raw_id": "demo-inc-001",
                    "source": "prometheus",
                    "timestamp": "2026-11-08T10:01:00",
                    "root_cause": "db-proxy 连接池耗尽导致支付链路超时",
                    "severity": "critical",
                },
                ensure_ascii=False,
            )
        if "「rca_synthesizer」" in system:
            return _ops_out(
                "critical",
                "high",
                suspected_components=["db-proxy"],
                suggested_actions=["重启 db-proxy 并扩容连接池", "对 payment-api 熔断降级"],
            )
        if "「generate_report」" in system:
            return _ops_out(
                "critical",
                "high",
                report_markdown="# 事故报告\n\n根因：db-proxy 连接池耗尽导致支付链路超时。",
                notify_channels=["oncall-im", "status-page"],
            )

        raise AssertionError(f"DemoResponder: unrecognized skill prompt: {system[:120]}")
