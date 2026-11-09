"""V2.7 测试：文档抽取节点（http/本地/data:/html）+ 低代码预处理接线回归 +
document_review 预置全链路 + CLI 多格式输入。"""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from vaf.app import VafApp
from vaf.errors import VafError
from vaf.llm.mock import ScriptedMockLLM
from vaf.presets import load_index, preset_path, try_preset
from vaf.preprocessor.document import DocumentExtractNode, extract_document_text
from vaf.preprocessor.nodes import RawItem


def _data_url(text: str) -> str:
    return "data:text/plain;base64," + base64.b64encode(text.encode()).decode()


# ---------------------------------------------------------------------------
# extract_document_text
# ---------------------------------------------------------------------------
def test_extract_data_url_plain_and_base64():
    assert extract_document_text(_data_url("你好文档")) == "你好文档"
    utf8 = "data:text/plain;charset=utf-8," + "常规文本"
    assert "常规文本" in extract_document_text(utf8)


def test_extract_html_strips_tags():
    html = _data_url("<html><script>var x=1;</script><body><h1>标题</h1><p>正文</p></body></html>")
    text = extract_document_text(html)
    assert "标题" in text and "正文" in text
    assert "var x" not in text  # script 剥除


def test_extract_local_file_and_missing(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# 标题\n内容", encoding="utf-8")
    assert "内容" in extract_document_text(str(f))
    with pytest.raises(ValueError, match="不存在"):
        extract_document_text(str(tmp_path / "ghost.txt"))
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 binary")
    with pytest.raises(ValueError, match="自定义 extractor"):
        extract_document_text(str(pdf))


def test_extract_http_url():
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = "<html><body>远程文档内容</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            captured["hit"] = True

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        text = extract_document_text(f"http://127.0.0.1:{server.server_port}/doc.html")
        assert "远程文档内容" in text and captured["hit"]
    finally:
        server.shutdown()


def test_document_extract_node_fallback_on_binary(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-binary")
    node = DocumentExtractNode()
    item = RawItem(id="i", payload={"document_url": str(pdf)})
    # fail_strategy=fallback：抽取失败时 enrich 抛出 → 预处理记录异常标记，不阻塞
    from vaf.preprocessor.pipeline import Preprocessor

    pre = Preprocessor()
    pre.enrichers = [node]
    result = pre.run([item])
    assert result[0].anomalies and "document_extract" in result[0].anomalies[0].source


def test_document_extract_node_custom_extractor():
    node = DocumentExtractNode(field="doc", output_field="text", extractor=lambda s: "PDF 抽取结果")
    out = node.enrich(RawItem(id="i", payload={"doc": "file:///any.pdf"}))
    assert out == {"text": "PDF 抽取结果"}


# ---------------------------------------------------------------------------
# document_review 预置全链路（data: URL 自包含）
# ---------------------------------------------------------------------------
def test_document_review_preset_runs_offline():
    results = try_preset("document_review")
    contract, report = results
    assert contract[1].status.value == "succeeded"
    assert contract[1].result["conclusion"] == "high_risk"
    assert contract[1].result["key_points"]
    assert report[1].result["conclusion"] == "medium_risk"
    assert any("应收" in c for c in report[1].result["concerns"])


def test_document_review_extract_really_ran():
    """端到端断言：预处理抽取确实发生（enriched 文本进入状态）。"""
    import yaml

    with open(preset_path("document_review").parent / "samples.yaml", encoding="utf-8") as fh:
        samples = yaml.safe_load(fh)
    from vaf.presets import PresetResponder

    app = VafApp()
    app.register_model_endpoint(
        "m", ScriptedMockLLM(name="m", responder=PresetResponder(samples["responses"])), tier=1
    )
    app.load_domain_file(str(preset_path("document_review")))
    handle = app.run("document_review", samples["inputs"][0]["input"], "doc-verify-1")
    assert handle.status.value == "succeeded"
    state = app.state_store.get("doc-verify-1")
    preprocessed = state.variables["_preprocessed"]
    assert preprocessed[0]["enriched_data"].get("document_text", "").startswith("服务协议")


# ---------------------------------------------------------------------------
# 低代码预处理接线回归（V2.2 起 spec.preprocessor 未执行的 bug 修复锁定）
# ---------------------------------------------------------------------------
def test_lowcode_preprocessor_is_executed_by_engine():
    spec = {
        "domain": "pre_check_demo", "version": "1.0.0",
        "input": {"fields": [{"name": "x", "type": "int", "required": True}]},
        "outputs": {"main": {"conclusion_enum": ["ok"]}},
        "preprocessor": {"validators": [{"type": "range", "field": "x", "low": 0, "high": 10, "level": "fatal"}]},
        "skills": [{"name": "s", "type": "analysis", "output": "main", "prompt": "{{ input.x }}"}],
        "pipeline": {"name": "pre_check_demo", "domain": "pre_check_demo",
                     "steps": [{"name": "st", "type": "skill", "skill": "s"}]},
        "result_from": "st",
    }
    app = VafApp()
    app.register_model_endpoint(
        "m", ScriptedMockLLM(name="m", responder=lambda *a, **k: json.dumps({
            "conclusion": "ok", "confidence": "high",
            "findings": [{"finding_id": "f1", "claim": "x", "evidence_refs": ["ev1"]}],
            "evidence": [{"ev_id": "ev1", "content": "x"}],
            "falsification": [{"signal": "s", "threshold": "t", "consequence": "c"}],
        })), tier=1)
    app.load_domain_dict(spec)
    handle = app.run("pre_check_demo",
                     {"raw_id": "pc-1", "source": "t", "timestamp": "0", "x": 99},
                     "precheck-1")
    assert handle.status.value == "succeeded"
    state = app.state_store.get("precheck-1")
    preprocessed = state.variables["_preprocessed"]
    # 越界（x=99 > high=10）必然产生 FATAL 异常标记 → 证明低代码预处理真的跑了
    assert preprocessed and preprocessed[0]["anomalies"]


def test_index_contains_document_review():
    names = {e["name"] for e in load_index()}
    assert "document_review" in names


# ---------------------------------------------------------------------------
# CLI 多格式输入
# ---------------------------------------------------------------------------
def test_load_input_payloads_formats(tmp_path):
    from vaf.cli import load_input_payloads

    j = tmp_path / "one.json"
    j.write_text(json.dumps({"raw_id": "1", "source": "s", "timestamp": "t"}), encoding="utf-8")
    assert load_input_payloads(str(j)) == [{"raw_id": "1", "source": "s", "timestamp": "t"}]

    jb = tmp_path / "batch.json"
    jb.write_text(json.dumps([
        {"raw_id": "1", "source": "s", "timestamp": "t"},
        {"raw_id": "2", "source": "s", "timestamp": "t"},
    ]), encoding="utf-8")
    assert len(load_input_payloads(str(jb))) == 2

    y = tmp_path / "form.yaml"
    y.write_text("raw_id: y\nsource: s\ntimestamp: t\n", encoding="utf-8")
    assert load_input_payloads(str(y))[0]["raw_id"] == "y"

    c = tmp_path / "rows.csv"
    c.write_text("raw_id,source,timestamp\nr1,s,t\nr2,s,t\n", encoding="utf-8")
    rows = load_input_payloads(str(c))
    assert len(rows) == 2 and rows[1]["raw_id"] == "r2"

    with pytest.raises(SystemExit):
        bad = tmp_path / "bad.csv"
        bad.write_text("raw_id\n", encoding="utf-8")
        load_input_payloads(str(bad))
