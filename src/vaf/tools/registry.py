"""工具注册表（DESIGN §15）：ToolSpec 声明幂等性 / 权限 / 副作用。"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..errors import ToolError

ToolFn = Callable[[Dict[str, Any]], Dict[str, Any]]


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    params_schema: Dict[str, Any] = Field(default_factory=dict)
    idempotent: bool = True
    required_permission: Optional[str] = None
    timeout_s: float = 10.0
    has_side_effect: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, tuple] = {}

    def register(self, fn: ToolFn, spec: ToolSpec, undo_fn: Optional[ToolFn] = None) -> None:
        """undo_fn：副作用工具的补偿函数（入参与调用时相同）；非幂等副作用工具建议提供。"""
        if spec.name in self._tools:
            raise ToolError(f"duplicate tool registration: {spec.name}")
        self._tools[spec.name] = (fn, spec, undo_fn)

    def spec(self, name: str) -> ToolSpec:
        entry = self._tools.get(name)
        if entry is None:
            raise ToolError(f"unknown tool: {name}")
        return entry[1]

    def fn(self, name: str) -> ToolFn:
        entry = self._tools.get(name)
        if entry is None:
            raise ToolError(f"unknown tool: {name}")
        return entry[0]

    def undo_fn(self, name: str) -> Optional[ToolFn]:
        entry = self._tools.get(name)
        return entry[2] if entry else None

    def definitions(self, allowed: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        out = []
        for name, (_, spec, _) in sorted(self._tools.items()):
            if allowed is not None and name not in allowed:
                continue
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.params_schema or {"type": "object", "properties": {}},
                    },
                }
            )
        return out

    def names(self) -> List[str]:
        return sorted(self._tools.keys())
