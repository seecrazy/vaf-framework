"""ScriptedMockLLM：确定性测试替身（DESIGN §8.4）。

- responder 签名：(messages, tools, response_schema, **opts) -> str | LLMResponse | ToolCallRequest 列表
- 队列脚本：依次弹出；为空时回退 default_responder
- 可注入故障：抛异常 / 返回非法 JSON / 返回违反 Schema 的 JSON（驱动自修复重试测试）
- 流式：将最终文本按 chunk_size 切片吐出
"""
from __future__ import annotations

import itertools
import json
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Union

from ..errors import VafError
from .client import LLMClient, LLMResponse, LLMUsage, Message, ToolCallRequest

Responder = Callable[..., Union[str, LLMResponse, List[ToolCallRequest]]]

_id_seq = itertools.count(1)


def _as_response(result: Union[str, LLMResponse, List[ToolCallRequest]], model: str) -> LLMResponse:
    if isinstance(result, LLMResponse):
        return result
    if isinstance(result, str):
        return LLMResponse(
            content=result,
            usage=LLMUsage(prompt_tokens=max(1, len(result) // 4), completion_tokens=max(1, len(result) // 4)),
            model=model,
        )
    # 工具调用列表
    return LLMResponse(
        tool_calls=list(result),
        finish_reason="tool_calls",
        usage=LLMUsage(prompt_tokens=32, completion_tokens=16),
        model=model,
    )


class ScriptedMockLLM(LLMClient):
    def __init__(
        self,
        name: str = "mock-llm",
        script: Optional[List[Union[str, LLMResponse, List[ToolCallRequest]]]] = None,
        responder: Optional[Responder] = None,
        capabilities: Sequence[str] = ("json", "tools", "stream"),
        chunk_size: int = 24,
    ):
        self.name = name
        self.capabilities = tuple(capabilities)
        self._script: List[Union[str, LLMResponse, List[ToolCallRequest]]] = list(script or [])
        self._responder = responder
        self.chunk_size = chunk_size
        self.call_count = 0
        self.last_messages: Optional[List[Message]] = None
        self.call_log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    def call(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        **opts: Any,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_messages = messages
        self.call_log.append(
            {
                "n": self.call_count,
                "has_tools": bool(tools),
                "has_schema": response_schema is not None,
                "messages": len(messages),
            }
        )
        if self._script:
            item = self._script.pop(0)
        elif self._responder is not None:
            item = self._responder(messages, tools, response_schema, **opts)
        else:
            raise VafError(f"ScriptedMockLLM '{self.name}' has no scripted response left")
        if isinstance(item, Exception):
            raise item
        return _as_response(item, self.name)

    def stream(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        **opts: Any,
    ) -> Iterator[str]:
        response = self.call(messages, tools=tools, **opts)
        text = response.content or ""
        for i in range(0, len(text), self.chunk_size):
            yield text[i : i + self.chunk_size]


def tool_call(name: str, arguments: Optional[Dict[str, Any]] = None) -> ToolCallRequest:
    """测试辅助：构造工具调用请求。"""
    return ToolCallRequest(
        call_id=f"call_{next(_id_seq)}", name=name, arguments=dict(arguments or {})
    )


def invalid_json(text: str = "这不是JSON，抱歉") -> str:
    return text


def schema_violating_json(**fields: Any) -> str:
    """返回缺字段的 JSON（用于驱动自修复重试）。"""
    return json.dumps(fields, ensure_ascii=False)
