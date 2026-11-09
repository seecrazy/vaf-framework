"""真实 LLM 零代码接入测试（V2.6）：内置 OpenAI 兼容客户端 + 环境变量/CLI 装配 +
本地假服务全链路（请求格式/鉴权头/流式/重试/工具调用/领域 run 走通）。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import VALID_DECISION  # noqa: F401
from vaf.errors import VafError
from vaf.llm.openai_compat import OpenAICompatClient, client_from_env







def _client(server):
    return OpenAICompatClient(server.base_url, "test-model", "sk-test", retries=2)


# ---------------------------------------------------------------------------
def test_call_request_format_and_parsing(fake_llm):
    resp = _client(fake_llm).call(
        [{"role": "user", "content": "hi"}], response_schema={"type": "object"}
    )
    assert json.loads(resp.content)["conclusion"] == "technical"
    assert resp.usage.prompt_tokens == 11 and resp.usage.total_tokens == 18
    req = fake_llm.requests[-1]
    assert req["path"].endswith("/chat/completions")
    assert req["auth"] == "Bearer sk-test"
    assert req["body"]["model"] == "test-model"
    assert req["body"]["response_format"] == {"type": "json_object"}  # json 能力协商生效


def test_tool_calls_parsed(fake_llm):
    resp = _client(fake_llm).call([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
    assert resp.tool_calls[0].name == "echo"
    assert resp.tool_calls[0].arguments == {"x": 1}


def test_stream_sse(fake_llm):
    chunks = list(_client(fake_llm).stream([{"role": "user", "content": "hi"}]))
    assert "".join(chunks) == "你好，世界"


def test_retry_on_429_then_success(fake_llm):
    fake_llm.fail_first_with_429 = True
    resp = _client(fake_llm).call([{"role": "user", "content": "hi"}])
    assert resp.content == VALID_DECISION
    assert len(fake_llm.requests) == 2  # 首次 429 → 重试成功


def test_non_retryable_error_raises(fake_llm):
    from conftest import FakeOpenAI

    class Always400(FakeOpenAI):
        def _make_handler(self):
            outer = self

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):  # noqa: N802
                    self.rfile.read(int(self.headers["Content-Length"]))
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error": "bad request"}')

                def log_message(self, *args):
                    pass

            return Handler

    server = Always400()
    try:
        with pytest.raises(VafError, match="HTTP 400"):
            OpenAICompatClient(server.base_url, "m", retries=0).call([{"role": "user", "content": "x"}])
    finally:
        server.close()


def test_client_from_env(monkeypatch):
    assert client_from_env({"VAF_LLM_MODEL": "only-model"}) is None  # 缺 BASE_URL
    monkeypatch.setenv("VAF_LLM_BASE_URL", "https://ark.example/api/v3")
    monkeypatch.setenv("VAF_LLM_MODEL", "doubao-1")
    monkeypatch.setenv("VAF_LLM_API_KEY", "k")
    client = client_from_env()
    assert client is not None and client.name == "llm-official"
    named = client_from_env({"VAF_LLM_BASE_URL": "http://x", "VAF_LLM_MODEL": "m", "VAF_LLM_NAME": "prod"})
    assert named.name == "prod"


# ---------------------------------------------------------------------------
# 全链路：零代码领域 × 真实 HTTP LLM
# ---------------------------------------------------------------------------
def test_zero_code_domain_runs_over_real_http_llm(fake_llm):
    """vaf run 同款装配（build_app + --llm 参数）跑零代码领域，LLM 走真实 HTTP。"""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for p in (str(root / "src"), str(root / "domains")):
        if p not in sys.path:
            sys.path.insert(0, p)

    from vaf.cli import build_app

    app = build_app(
        llm_base_url=fake_llm.base_url,
        llm_api_key="sk-test",
        llm_model="test-model",
    )
    assert "llm-official" in app.model_router.endpoint_names()
    assert "demo-mock" not in app.model_router.endpoint_names()  # 配置后 Mock 不注册

    handle = app.run(
        "customer_ticket",
        {"raw_id": "real-1", "source": "app", "timestamp": "2026-11-09T10:00:00",
         "subject": "实例无法启动", "body": "开机报错 500", "order_amount": 0},
        request_id="real-llm-1",
    )
    assert handle.status.value == "succeeded", handle.error
    assert handle.result["conclusion"] == "technical"
    # 真实请求发出（含系统提示词与输出 Schema 协商）
    req = fake_llm.requests[-1]
    assert "response_format" in req["body"]


def test_build_app_env_fallback_registers_official(monkeypatch, fake_llm):
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for p in (str(root / "src"), str(root / "domains")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from vaf.cli import build_app

    monkeypatch.setenv("VAF_LLM_BASE_URL", fake_llm.base_url)
    monkeypatch.setenv("VAF_LLM_MODEL", "env-model")
    app = build_app()  # 环境变量装配（零 CLI 参数）
    assert "llm-official" in app.model_router.endpoint_names()


def test_build_app_without_config_keeps_offline_mock():
    import os
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for p in (str(root / "src"), str(root / "domains")):
        if p not in sys.path:
            sys.path.insert(0, p)
    saved = {k: os.environ.pop(k, None) for k in
             ("VAF_LLM_BASE_URL", "VAF_LLM_MODEL", "VAF_LLM_API_KEY", "VAF_LLM_NAME")}
    try:
        from vaf.cli import build_app

        app = build_app()
        assert "demo-mock" in app.model_router.endpoint_names()
    finally:
        for k, v in saved.items():
            if v:
                os.environ[k] = v
