"""内置 OpenAI 兼容客户端（V2.6）：接真实 LLM 零代码。

OpenAI `/chat/completions` 协议已是事实标准（火山方舟 / OpenAI / DeepSeek /
GLM / Qwen / Kimi 等均兼容）——接入退化为三个配置：

    export VAF_LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
    export VAF_LLM_API_KEY=xxxx
    export VAF_LLM_MODEL=doubao-seed-1.6

仅用标准库（urllib），不引入任何 SDK 依赖；直连绕过系统代理；
429/5xx/网络错误自动重试（2 次退避）。非 OpenAI 兼容供应商仍可手写
LLMClient 注册（逃生舱不变）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

from ..errors import VafError
from .client import LLMClient, LLMResponse, LLMUsage, Message, ToolCallRequest

# 内网/本机直连：显式绕过系统代理（与 webhook/cards 同一策略）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_RETRYABLE_STATUS = (429, 500, 502, 503, 504)


class OpenAICompatClient(LLMClient):
    name = "openai-compat"
    capabilities = ("json", "tools", "stream")

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        *,
        endpoint_name: str = "llm-official",
        timeout_s: float = 120.0,
        retries: int = 2,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self.name = endpoint_name
        self._timeout = timeout_s
        self._retries = retries
        self._extra_headers = extra_headers or {}

    # ------------------------------------------------------------------
    def call(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        **opts: Any,
    ) -> LLMResponse:
        body: Dict[str, Any] = {"model": self._model, "messages": messages}
        if response_schema is not None:
            body["response_format"] = {"type": "json_object"}
        if tools:
            body["tools"] = tools
        data = self._post(body)
        msg = data["choices"][0]["message"]
        tool_calls = [
            ToolCallRequest(
                call_id=tc.get("id", ""),
                name=tc["function"]["name"],
                arguments=json.loads(tc["function"].get("arguments") or "{}"),
            )
            for tc in msg.get("tool_calls") or []
        ]
        usage = data.get("usage") or {}
        return LLMResponse(
            content=msg.get("content"),
            tool_calls=tool_calls,
            usage=LLMUsage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
            finish_reason=data["choices"][0].get("finish_reason", "stop"),
            model=data.get("model", self._model),
        )

    def stream(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        **opts: Any,
    ) -> Iterator[str]:
        body: Dict[str, Any] = {"model": self._model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
        request = self._build_request(body)
        try:
            with _opener.open(request, timeout=self._timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
        except urllib.error.HTTPError as exc:
            raise VafError(self._http_error(exc)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise VafError(f"LLM 流式请求失败（{self._url}）：{exc}") from exc

    # ------------------------------------------------------------------
    def _build_request(self, body: Dict[str, Any]) -> urllib.request.Request:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers.update(self._extra_headers)
        return urllib.request.Request(
            self._url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        last_error = ""
        for attempt in range(self._retries + 1):
            request = self._build_request(body)
            try:
                with _opener.open(request, timeout=self._timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                last_error = self._http_error(exc, detail)
                if exc.code not in _RETRYABLE_STATUS:
                    raise VafError(last_error) from exc
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                last_error = f"LLM 请求失败（{self._url}）：{exc}"
            if attempt < self._retries:
                time.sleep(0.5 * (attempt + 1))
        raise VafError(last_error or f"LLM 请求失败（{self._url}）")

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError, detail: str = "") -> str:
        message = f"LLM 服务返回 HTTP {exc.code}（{exc.url}）"
        if detail:
            message += f"：{detail}"
        return message


def client_from_env(env: Optional[Dict[str, str]] = None) -> Optional[OpenAICompatClient]:
    """从环境变量装配客户端；BASE_URL 与 MODEL 齐备才生效，否则返回 None。

    VAF_LLM_BASE_URL / VAF_LLM_API_KEY / VAF_LLM_MODEL
    可选：VAF_LLM_NAME（端点名，默认 llm-official）
    """
    source = env if env is not None else os.environ
    base_url = source.get("VAF_LLM_BASE_URL", "").strip()
    model = source.get("VAF_LLM_MODEL", "").strip()
    if not base_url or not model:
        return None
    return OpenAICompatClient(
        base_url,
        model,
        source.get("VAF_LLM_API_KEY", "").strip(),
        endpoint_name=source.get("VAF_LLM_NAME", "").strip() or "llm-official",
    )
