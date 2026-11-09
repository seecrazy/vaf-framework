"""LLM Client 协议与能力协商（DESIGN §8.1）。

capabilities ⊆ {"json", "tools", "stream"}：
- json：支持 response_schema 结构化输出；
- tools：支持函数/工具调用；
- stream：支持流式输出。
执行器按能力降级：无 json → Schema 注入 prompt + 容错解析；无 tools → 禁用工具循环。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ToolCallRequest:
    call_id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    content: Optional[str] = None
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    usage: Optional[LLMUsage] = None
    finish_reason: str = "stop"
    model: str = ""


Message = Dict[str, Any]  # OpenAI 风格消息结构


class LLMClient(ABC):
    """LLM 客户端抽象。实现方负责自身重试与超时，框架层负责修复重试与降级。"""

    name: str = ""
    capabilities: Sequence[str] = ()

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    @abstractmethod
    def call(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        **opts: Any,
    ) -> LLMResponse:  # pragma: no cover - interface
        raise NotImplementedError

    def stream(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        **opts: Any,
    ) -> Iterator[str]:  # pragma: no cover - default unsupported
        raise NotImplementedError(f"{type(self).__name__} does not support streaming")
