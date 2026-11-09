"""V2.8 测试：检索节点（规则库/BM25-lite/注入 retriever/相对路径）+
credit 深度编排旗舰（四终局）+ contract 规则库注入 + 黄金集评估机制。"""
import json

import pytest

from vaf.app import VafApp
from vaf.errors import VafError
from vaf.presets import preset_eval, preset_path, try_preset
from vaf.preprocessor.retrieval import RetrievalNode, keyword_rank, load_knowledge_base


KB = "entries:\n  - id: a\n    keywords: [dti]\n    text: 规则A\n  - id: b\n    keywords: [管辖]\n    text: 规则B\n"


def test_keyword_rank_ordering(tmp_path):
    import yaml

    path = tmp_path / "kb.yaml"
    path.write_text(KB, encoding="utf-8")
    entries = load_knowledge_base(str(path))
    hits = keyword_rank(entries, "DTI 超标案例", top_k=2)
    assert [e["id"] for e in hits] == ["a"]
    assert keyword_rank(entries, "无关内容", 2) == []


def test_retrieval_node_lazy_load_and_output(tmp_path):
    path = tmp_path / "kb.yaml"
    path.write_text(KB, encoding="utf-8")
    node = RetrievalNode(str(path), top_k=1)
    from vaf.preprocessor.nodes import RawItem

    out = node.enrich(RawItem(id="i", payload={"note": "关注 DTI 与收入"}))
    assert out == {"knowledge": ["规则A"]}


def test_retrieval_node_missing_file():
    with pytest.raises(VafError, match="不存在"):
        RetrievalNode("/no/such/kb.yaml").enrich(
            __import__("vaf.preprocessor.nodes", fromlist=["RawItem"]).RawItem(id="i", payload={})
        )


def test_retrieval_node_custom_retriever():
    node = RetrievalNode("ignored.yaml", retriever=lambda q, k: [f"向量检索:{q[:4]}"][:k])
    from vaf.preprocessor.nodes import RawItem

    out = node.enrich(RawItem(id="i", payload={"x": "查询"}))
    assert out["knowledge"]


def test_knowledge_base_missing_text(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("entries:\n  - id: x\n    keywords: []\n", encoding="utf-8")
    with pytest.raises(VafError, match="缺少 text"):
        load_knowledge_base(str(p))


# ---------------------------------------------------------------------------
# credit_review 深度编排旗舰
# ---------------------------------------------------------------------------
def test_credit_deep_pipeline_four_endings():
    results = try_preset("credit_review")
    by_name = {name: h for name, h in results}
    assert by_name["良好申请人（DTI 低、征信优 → 直通 pre_approved）"].status.value == "succeeded"
    assert by_name["高负债申请人（DTI 灰区 + 征信一般 → manual_review）"].result["conclusion"] == "manual_review"
    assert by_name["大额预批（150 万 → 触发人工审核暂停）"].status.value == "waiting_review"
    assert by_name["失业申请人（无收入佐证 → 触发澄清暂停）"].status.value == "waiting_input"


def test_credit_retrieval_injects_knowledge_into_prompt():
    import yaml

    samples = yaml.safe_load(
        open(preset_path("credit_review").parent / "samples.yaml", encoding="utf-8")
    )
    from vaf.presets import PresetResponder
    from vaf.llm.mock import ScriptedMockLLM

    captured = {}
    app = VafApp()

    class CaptureResponder(PresetResponder):
        def __call__(self, messages, tools, response_schema, **opts):
            captured.setdefault("prompts", []).append(messages[0]["content"])
            return super().__call__(messages, tools, response_schema, **opts)

    app.register_model_endpoint(
        "m", ScriptedMockLLM(name="m", responder=CaptureResponder(samples["responses"])), tier=1
    )
    app.load_domain_file(str(preset_path("credit_review")))
    h = app.run("credit_review", samples["inputs"][0]["input"], "retr-1")
    assert h.status.value == "succeeded"
    # 规则库命中并注入提示词
    assert any("检索到的适用规则" in p and "DTI" in p for p in captured["prompts"])


def test_contract_retrieval_injects_legal_rules():
    import yaml

    samples = yaml.safe_load(
        open(preset_path("contract_risk_review").parent / "samples.yaml", encoding="utf-8")
    )
    from vaf.presets import PresetResponder
    from vaf.llm.mock import ScriptedMockLLM

    app = VafApp()
    app.register_model_endpoint(
        "m", ScriptedMockLLM(name="m", responder=PresetResponder(samples["responses"])), tier=1
    )
    app.load_domain_file(str(preset_path("contract_risk_review")))
    h = app.run("contract_risk_review", samples["inputs"][0]["input"], "retr-2")
    assert h.status.value == "succeeded"
    state = app.state_store.get("retr-2")
    knowledge = state.variables["_preprocessed"][0]["enriched_data"].get("knowledge", [])
    assert any("585" in k for k in knowledge)  # 违约金调减规则命中
    assert any("管辖" in k for k in knowledge)


# ---------------------------------------------------------------------------
# 黄金集评估
# ---------------------------------------------------------------------------
def test_preset_eval_offline_credit_full_marks():
    report, mode = preset_eval("credit_review")
    assert mode == "offline"
    assert report.metrics["conclusion_accuracy"] == 1.0
    assert report.gate_passed
    assert report.total == 4  # offline 只跑 samples；golden.yaml 仅真实 LLM 模式参与


def test_preset_eval_offline_contract_and_investment():
    for name in ("contract_risk_review", "investment_research_note"):
        report, mode = preset_eval(name)
        assert mode == "offline" and report.gate_passed, (name, report.failed_gates)


def test_preset_eval_live_mode_with_fake_llm(monkeypatch, fake_llm):
    """配置 VAF_LLM_* 后进入 live 模式：golden.yaml 用例一并参与。"""
    import os

    monkeypatch.setenv("VAF_LLM_BASE_URL", fake_llm.base_url)
    monkeypatch.setenv("VAF_LLM_MODEL", "test-model")
    import yaml

    golden = yaml.safe_load(
        open(preset_path("credit_review").parent / "golden.yaml", encoding="utf-8")
    )
    samples = yaml.safe_load(
        open(preset_path("credit_review").parent / "samples.yaml", encoding="utf-8")
    )
    report, mode = preset_eval("credit_review")
    assert mode == "live"
    assert report.total == len(samples["inputs"]) + len(golden["cases"])  # samples + golden 全参与
    # 假服务返回固定响应——多数用例契约不匹配而失败，此处验证 live 机制（数量），非常数断言准确率


def test_golden_yaml_inputs_pass_contract():
    """golden.yaml 的扩充用例本身必须通过输入契约（防手写错例）。"""
    import yaml

    from vaf.llm.mock import ScriptedMockLLM

    app = VafApp()
    app.register_model_endpoint("m", ScriptedMockLLM(name="m", responder=lambda *a, **k: "{}"), tier=1)
    app.load_domain_file(str(preset_path("credit_review")))
    reg = app.engine.domain_of("credit_review")
    golden = yaml.safe_load(
        open(preset_path("credit_review").parent / "golden.yaml", encoding="utf-8")
    )
    for case in golden["cases"]:
        validated = reg.input_contract.model_validate(case["input"])
        assert validated.raw_id
