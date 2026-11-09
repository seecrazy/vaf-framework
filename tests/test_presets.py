"""预置领域库测试（V2.3）：目录完整性 / 三个预置离线跑通 / 证伪触发 /
install / 代码风格孪生包（含人工审核路径）。"""
import json
import sys
from pathlib import Path

import pytest

from vaf.app import VafApp
from vaf.errors import VafError
from vaf.llm.mock import ScriptedMockLLM
from vaf.presets import (
    install_preset,
    load_index,
    preset_path,
    presets_dir,
    try_preset,
)
from vaf.security.permissions import User

REVIEWER = User("credit-officer", {"reviewer"})


# ---------------------------------------------------------------- 目录完整性
def test_index_lists_all_presets_with_knowledge():
    entries = load_index()
    names = {e["name"] for e in entries}
    assert {"credit_review", "contract_risk_review", "financial_report_extract"} <= names
    for entry in entries:
        assert entry["description"]
        assert entry.get("knowledge", "").strip(), f"{entry['name']} 缺少知识依据声明"
        assert preset_path(entry["name"]).exists()


def test_presets_dir_resolved():
    assert (presets_dir() / "index.yaml").exists()


# ---------------------------------------------------------------- 离线体验
def test_credit_review_preset_runs_offline():
    results = try_preset("credit_review")
    assert [h.status.value for _, h in results] == ["succeeded", "succeeded"]
    assert results[0][1].result["conclusion"] == "pre_approved"
    assert results[1][1].result["conclusion"] == "manual_review"
    assert results[1][1].result["suggested_adjustments"]


def test_contract_risk_review_preset_runs_offline():
    results = try_preset("contract_risk_review")
    assert results[0][1].result["conclusion"] == "high_risk"
    assert results[1][1].result["conclusion"] == "low_risk"


def test_financial_report_extract_preset_runs_offline():
    results = try_preset("financial_report_extract")
    fast_report = results[0][1]
    formal = results[1][1]
    assert fast_report.result["conclusion"] == "partial"
    assert fast_report.result["revision_warning"] is True
    # 数据缺口强制声明（快报缺扣非）
    assert formal.result["conclusion"] == "complete"


def test_credit_falsification_checker_triggers_on_low_score():
    """评分 < 450 命中表达式校验器 → 证伪触发留痕（处置闭环入口）。"""
    import yaml

    with open(preset_path("credit_review").parent / "samples.yaml", encoding="utf-8") as fh:
        samples = yaml.safe_load(fh)
    from vaf.presets import PresetResponder

    app = VafApp()
    app.register_model_endpoint(
        "m", ScriptedMockLLM(name="m", responder=PresetResponder(samples["responses"])), tier=1
    )
    app.load_domain_file(str(preset_path("credit_review")))
    risky = dict(samples["inputs"][0]["input"])
    risky.update(raw_id="cr-falsify", credit_score=420)
    h = app.run("credit_review", risky, "preset-falsify-1")
    assert h.status.value in ("succeeded", "failed")
    # 无论 LLM 给何结论，实时回检已触发（评分 420 < 450）
    assert "征信复查评分显著下降" in h.falsification_flags


# ---------------------------------------------------------------- 安装
def test_install_preset_copies_and_refuses_overwrite(tmp_path):
    dest = install_preset("credit_review", target_dir=str(tmp_path))
    assert dest.exists() and dest.name == "credit_review.domain.yaml"
    # 安装后的文件可独立加载（0 分钟使用路径）
    app = VafApp()
    app.register_model_endpoint(
        "m", ScriptedMockLLM(name="m", responder=lambda *a, **k: "{}"), tier=1
    )
    name = app.load_domain_file(str(dest))
    assert name == "credit_review"
    with pytest.raises(VafError, match="已存在"):
        install_preset("credit_review", target_dir=str(tmp_path))


def test_try_preset_missing_samples(tmp_path):
    # 构造一个缺 samples 的假预置目录
    src = preset_path("credit_review")
    fake_root = tmp_path / "presets"
    (fake_root / "mini").mkdir(parents=True)
    (fake_root / "index.yaml").write_text(
        "presets: [{name: mini, description: d, knowledge: k}]\n", encoding="utf-8"
    )
    import shutil

    shutil.copyfile(src, fake_root / "mini" / "mini.domain.yaml")
    with pytest.raises(VafError, match="samples"):
        try_preset("mini", presets_root=str(fake_root))


# ---------------------------------------------------------------- 代码风格孪生
def _decision_json(conclusion, confidence="high", dti=0.3):
    return json.dumps(
        {
            "conclusion": conclusion,
            "confidence": confidence,
            "dti_computed": dti,
            "risk_factors": [],
            "suggested_adjustments": [],
            "findings": [
                {"finding_id": "f1", "claim": "预审依据", "evidence_refs": ["ev1"]},
            ],
            "evidence": [
                {"ev_id": "ev1", "content": "收入与征信数据", "source_ref": "s1"},
            ],
            "sources": [{"ref_id": "s1", "grade": "L1", "label": "申请材料"}],
            "falsification": [
                {
                    "signal": "征信复查评分显著下降",
                    "threshold": "复查评分 < 450",
                    "consequence": "结论失效转人工",
                    "check_frequency": "realtime",
                    "checker_id": "credit_score_recheck",
                }
            ],
            "data_classification": "sensitive",
        },
        ensure_ascii=False,
    )


def _py_twin_app():
    pkg_parent = Path(__file__).resolve().parent.parent / "presets" / "credit_review"
    sys.path.insert(0, str(pkg_parent))
    app = VafApp()
    app.register_model_endpoint(
        "m",
        ScriptedMockLLM(name="m", responder=lambda *a, **k: _decision_json("pre_approved")),
        tier=1,
    )
    import credit_review_python  # noqa: F401  (presets 内孪生包)

    credit_review_python.register(app)
    return app


BASE_APP = {
    "raw_id": "py-1", "source": "loan_app", "timestamp": "0",
    "loan_amount": 300000, "term_months": 36,
    "monthly_income": 25000, "monthly_debt_payment": 2000,
    "credit_score": 680, "employment": "stable_employed",
    "loan_purpose": "装修",
}


def test_python_twin_small_loan_direct_approval():
    app = _py_twin_app()
    h = app.run("credit_review_python", BASE_APP, "py-twin-1")
    assert h.status.value == "succeeded", h.error
    assert h.result["conclusion"] == "pre_approved"
    # 预处理 DTI 推算确实发生（enriched 数据进入提示词，终审链路完整）
    state = app.state_store.get("py-twin-1")
    preprocessed = state.variables["_preprocessed"]
    assert preprocessed[0]["enriched_data"].get("dti") is not None


def test_python_twin_high_amount_triggers_human_review():
    app = _py_twin_app()
    big = {**BASE_APP, "raw_id": "py-2", "loan_amount": 1500000}
    h = app.run("credit_review_python", big, "py-twin-2")
    assert h.status.value == "waiting_review", h.error
    assert h.pending_review_task
    h = app.submit_review(h.pending_review_task, "approved", reviewer=REVIEWER, comment="大额人工复核通过")
    assert h.status.value == "succeeded", h.error
    assert h.result["conclusion"] == "pre_approved"
    state = app.state_store.get("py-twin-2")
    assert state.step_states["high_amount_review"].status.value == "succeeded"
