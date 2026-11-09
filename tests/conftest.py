import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "src"), os.path.join(ROOT, "domains")):
    if p not in sys.path:
        sys.path.insert(0, p)

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from e2e_helpers import make_app


VALID_DECISION = json.dumps({
    "conclusion": "technical", "confidence": "high",
    "subcategory": "实例无法启动", "severity": "medium",
    "findings": [{"finding_id": "f1", "claim": "无法启动", "evidence_refs": ["ev1"]}],
    "evidence": [{"ev_id": "ev1", "content": "工单正文", "source_ref": "s1"}],
    "sources": [{"ref_id": "s1", "grade": "L2", "label": "用户工单"}],
    "falsification": [{"signal": "已解决", "threshold": "subject 含已解决", "consequence": "关闭工单"}],
}, ensure_ascii=False)

VALID_ENTITIES = json.dumps(
    {"mentioned_products": ["云主机"], "amount_claimed": 0}, ensure_ascii=False
)


def _content_for(messages) -> str:
    """按技能名标记返回对应契约的合法响应（系统提示词含「技能名」）。"""
    joined = json.dumps(messages, ensure_ascii=False)
    return VALID_ENTITIES if "「entity_extract」" in joined else VALID_DECISION


class FakeOpenAI:
    """OpenAI 兼容假服务（记录请求，可注入 429 首次失败）。"""

    def __init__(self):
        self.requests = []
        self.fail_first_with_429 = False
        server = HTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = server.server_port
        self._server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization", ""),
                    "body": body,
                })
                if outer.fail_first_with_429:
                    outer.fail_first_with_429 = False
                    self.send_response(429)
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for piece in ("你好", "，", "世界"):
                        chunk = {"choices": [{"delta": {"content": piece}}]}
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                payload = {
                    "model": body.get("model"),
                    "choices": [{
                        "message": {"content": _content_for(body.get("messages", [])), "tool_calls": (
                            [{"id": "c1", "type": "function",
                              "function": {"name": "echo", "arguments": "{\"x\": 1}"}}]
                            if body.get("tools") else []
                        )},
                        "finish_reason": "tool_calls" if body.get("tools") else "stop",
                    }],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                }
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # 静默
                pass

        return Handler

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        self._server.shutdown()


@pytest.fixture()
def fake_llm():
    server = FakeOpenAI()
    yield server
    server.close()


@pytest.fixture()
def build_medical_app():
    return make_app()


@pytest.fixture()
def build_ops_app():
    return make_app()
