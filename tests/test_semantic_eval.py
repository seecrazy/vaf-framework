"""V2.9 测试：向量检索（假 embedding 服务 / auto 回退 / 余弦）+
法律与投资预置检索注入 + 全部预置 golden/expected 用例契约校验与离线评估。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from vaf.errors import VafError
from vaf.llm.embedding import (
    EmbeddingClient,
    EmbeddingRetriever,
    cosine_similarity,
    embedding_client_from_env,
    make_auto_retriever,
)
from vaf.presets import load_index, preset_eval, preset_path, presets_dir, samples_path
from vaf.preprocessor.retrieval import RetrievalNode, load_knowledge_base


# ---------------------------------------------------------------------------
# 假 embedding 服务：字符 bigram 词袋向量（同词文本→向量相近，可测排序）
# ---------------------------------------------------------------------------
def _bow_vector(text: str, dim: int = 64) -> list:
    vec = [0.0] * dim
    tokens = [text[i : i + 2] for i in range(len(text) - 1)] + [c for c in text]
    for t in tokens:
        vec[hash(t) % dim] += 1.0
    return vec


class FakeEmbeddingServer:
    def __init__(self):
        server = HTTPServer(("127.0.0.1", 0), self._handler())
        self.port = server.server_port
        self._server = server
        self.calls = []
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append(body["input"])
                data = [
                    {"object": "embedding", "index": i, "embedding": _bow_vector(t)}
                    for i, t in enumerate(body["input"])
                ]
                payload = json.dumps({"data": data, "model": body.get("model")}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        return Handler

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        self._server.shutdown()


@pytest.fixture()
def fake_embedding():
    server = FakeEmbeddingServer()
    yield server
    server.close()


def test_cosine_similarity_basics():
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([0, 0], [1, 1]) == 0.0


def test_embedding_client_request_and_parse(fake_embedding):
    client = EmbeddingClient(fake_embedding.base_url, "emb-test", "k")
    vectors = client.embed(["违约金条款", "管辖条款"])
    assert len(vectors) == 2 and len(vectors[0]) == 64
    assert fake_embedding.calls[0] == ["违约金条款", "管辖条款"]


def test_embedding_retriever_semantic_ranking(fake_embedding):
    client = EmbeddingClient(fake_embedding.base_url, "emb-test")
    texts = ["违约金过分高于损失可请求调减", "协议管辖须与争议有实际联系", "保密期限建议三到五年"]
    retriever = EmbeddingRetriever(client, texts)
    hits = retriever.top_k("关于违约金过高的调整", 2)
    assert hits[0] == "违约金过分高于损失可请求调减"  # 词袋相近 → 语义排序正确
    # 条目向量缓存：首批调用=全部条目一次性嵌入，其后每次调用仅 1 条查询
    retriever.top_k("管辖", 1)
    assert fake_embedding.calls[0] == texts  # 条目仅嵌入一次
    assert all(len(call) == 1 for call in fake_embedding.calls[1:])  # 其后为单查询


def test_make_auto_retriever_env(monkeypatch, fake_embedding):
    assert make_auto_retriever(["a"], env={}) is None  # 未配置 → 回退关键词
    retriever = make_auto_retriever(
        ["违约金调减规则"],
        env={
            "VAF_EMBEDDING_BASE_URL": fake_embedding.base_url,
            "VAF_EMBEDDING_MODEL": "emb-test",
        },
    )
    assert retriever is not None


def test_embedding_client_from_env(monkeypatch):
    assert embedding_client_from_env({}) is None
    client = embedding_client_from_env(
        {"VAF_EMBEDDING_BASE_URL": "https://x", "VAF_EMBEDDING_MODEL": "m"}
    )
    assert client is not None


def test_retrieval_node_auto_uses_embedding(monkeypatch, tmp_path, fake_embedding):
    """配置 VAF_EMBEDDING_* 后，auto 模式 RetrievalNode 走向量路径。"""
    from vaf.preprocessor.nodes import RawItem

    kb = tmp_path / "kb.yaml"
    kb.write_text(
        "entries:\n"
        "  - id: a\n    keywords: [zzz]\n    text: 违约金过分高于损失可请求调减\n"
        "  - id: b\n    keywords: [yyy]\n    text: 协议管辖须与争议有实际联系\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VAF_EMBEDDING_BASE_URL", fake_embedding.base_url)
    monkeypatch.setenv("VAF_EMBEDDING_MODEL", "emb-test")
    node = RetrievalNode(str(kb), top_k=1, mode="auto")
    out = node.enrich(RawItem(id="i", payload={"q": "违约金过高怎么调"}))
    # 关键词 zzz/yyy 均不命中——关键词检索必空；向量检索命中违约金条目
    assert out == {"knowledge": ["违约金过分高于损失可请求调减"]}


def test_retrieval_node_keyword_mode_forces_fallback(monkeypatch, tmp_path, fake_embedding):
    from vaf.preprocessor.nodes import RawItem

    kb = tmp_path / "kb.yaml"
    kb.write_text(
        "entries:\n  - id: a\n    keywords: [dti]\n    text: 规则A\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VAF_EMBEDDING_BASE_URL", fake_embedding.base_url)
    monkeypatch.setenv("VAF_EMBEDDING_MODEL", "emb-test")
    node = RetrievalNode(str(kb), top_k=1, mode="keyword")
    out = node.enrich(RawItem(id="i", payload={"q": "DTI 检查"}))
    assert out == {"knowledge": ["规则A"]}  # 强制关键词：不调 embedding
    assert not fake_embedding.calls


# ---------------------------------------------------------------------------
# 法律/投资预置检索注入
# ---------------------------------------------------------------------------
def test_legal_knowledge_injected():
    import yaml

    from vaf.app import VafApp
    from vaf.llm.mock import ScriptedMockLLM
    from vaf.presets import PresetResponder

    samples = yaml.safe_load(open(samples_path("legal_case_assessment"), encoding="utf-8"))
    app = VafApp()
    app.register_model_endpoint(
        "m", ScriptedMockLLM(name="m", responder=PresetResponder(samples["responses"])), tier=1
    )
    app.load_domain_file(str(preset_path("legal_case_assessment")))
    h = app.run("legal_case_assessment", samples["inputs"][0]["input"], "kb-legal-1")
    assert h.status.value == "succeeded"
    state = app.state_store.get("kb-legal-1")
    knowledge = state.variables["_preprocessed"][0]["enriched_data"].get("knowledge", [])
    assert any("高度盖然性" in k or "举证" in k for k in knowledge)  # 案件评估规则命中
    assert any("时效" in k for k in knowledge)


def test_investment_knowledge_injected():
    import yaml

    from vaf.app import VafApp
    from vaf.llm.mock import ScriptedMockLLM
    from vaf.presets import PresetResponder

    samples = yaml.safe_load(open(samples_path("investment_research_note"), encoding="utf-8"))
    app = VafApp()
    app.register_model_endpoint(
        "m", ScriptedMockLLM(name="m", responder=PresetResponder(samples["responses"])), tier=1
    )
    app.load_domain_file(str(preset_path("investment_research_note")))
    h = app.run("investment_research_note", samples["inputs"][1]["input"], "kb-iv-1")
    assert h.status.value == "succeeded"
    state = app.state_store.get("kb-iv-1")
    knowledge = state.variables["_preprocessed"][0]["enriched_data"].get("knowledge", [])
    assert knowledge  # 检索发生且命中
    # 样例核心主题（现金流背离）规则命中（商誉条目按分值竞争排序，非必进 top3）
    assert any("现金流" in k for k in knowledge)


# ---------------------------------------------------------------------------
# 全部预置：golden/expected 用例过输入契约 + 离线评估
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entry", [e["name"] for e in load_index()], ids=lambda n: n)
def test_all_preset_eval_offline_passes(entry):
    report, mode = preset_eval(entry)
    assert mode == "offline"
    assert report.gate_passed, (entry, report.failed_gates)


@pytest.mark.parametrize("entry", [e["name"] for e in load_index()], ids=lambda n: n)
def test_all_preset_cases_validate_against_contract(entry):
    """samples.inputs 与 golden.yaml 全部用例必须通过领域输入契约（防手写错例）。"""
    import yaml

    from vaf.app import VafApp
    from vaf.llm.mock import ScriptedMockLLM

    app = VafApp()
    app.register_model_endpoint("m", ScriptedMockLLM(name="m", responder=lambda *a, **k: "{}"), tier=1)
    from vaf.presets import preset_kind, package_dir, presets_dir

    if preset_kind(entry) == "lowcode":
        app.load_domain_file(str(preset_path(entry)))
        reg = app.engine.domain_of(entry)
    else:
        import importlib
        import sys

        pkg_root = str(presets_dir() / entry)
        if pkg_root not in sys.path:
            sys.path.insert(0, pkg_root)
        importlib.import_module(entry).register(app)
        reg = app.engine.domain_of(entry)

    samples = yaml.safe_load(open(samples_path(entry), encoding="utf-8")) or {}
    for sample in samples.get("inputs", []):
        reg.input_contract.model_validate(sample["input"])
    golden_file = presets_dir() / entry / "golden.yaml"
    if golden_file.is_file():
        golden = yaml.safe_load(open(golden_file, encoding="utf-8")) or {}
        for case in golden.get("cases", []):
            reg.input_contract.model_validate(case["input"])


def test_golden_case_totals_reach_40():
    """三领域黄金集合计 ≥ 40 例（samples.expected + golden）。"""
    import yaml

    total = 0
    for item in load_index():
        entry = item["name"]
        samples = yaml.safe_load(open(samples_path(entry), encoding="utf-8")) or {}
        total += sum(1 for s in samples.get("inputs", []) if s.get("expected"))
        golden_file = presets_dir() / entry / "golden.yaml"
        if golden_file.is_file():
            golden = yaml.safe_load(open(golden_file, encoding="utf-8")) or {}
            total += len(golden.get("cases", []))
    assert total >= 40, f"当前 {total} 例"
