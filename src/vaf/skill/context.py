"""SkillContext 与 StateView（DESIGN §6.3 / C9）：技能可见的全类型化上下文 + 字段级 ACL。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from ..errors import PermissionDeniedError
from ..contract.base import DomainInput
from ..preprocessor.nodes import PreprocessedItem
from .memory import ErrorRule

DEFAULT_STATE_ACL = ("steps.*.output", "vars.public.*")


class StateView:
    """pipeline 状态的只读、ACL 受限视图。

    技能只能读取 state_acl 声明前缀覆盖的路径；越权读取抛 PermissionDeniedError。
    路径语法：steps.<step>.output.<field...> / vars.<key>（dict 逐级下钻）。
    """

    def __init__(self, allowed_prefixes: List[str], backing: Dict[str, Any]):
        self._prefixes = list(allowed_prefixes or DEFAULT_STATE_ACL)
        self._backing = backing

    def _resolve(self, path: str) -> Any:
        parts = path.split(".")
        node: Any = self._backing
        for part in parts:
            if isinstance(node, dict):
                if part not in node:
                    raise KeyError(f"state path not found: {path} (missing '{part}')")
                node = node[part]
            elif isinstance(node, BaseModel):
                if part not in type(node).model_fields:
                    raise KeyError(f"state path not found: {path} (missing '{part}')")
                node = getattr(node, part)
            else:
                raise KeyError(f"state path not resolvable at '{part}': {path}")
        return node

    def _allowed(self, path: str) -> bool:
        """段级通配匹配：前缀 'steps.*.output' 允许 steps.<任意>.output.<更深路径>；
        尾部 '.*' 等价于该前缀下任意子路径。"""
        segs = path.split(".")
        for prefix in self._prefixes:
            psegs = prefix.split(".")
            if len(psegs) > len(segs):
                continue
            if all(p == "*" or p == s for p, s in zip(psegs, segs)):
                return True
        return False

    def get(self, path: str, default: Any = None) -> Any:
        if not self._allowed(path):
            raise PermissionDeniedError(
                f"state path '{path}' is outside the skill's state_acl {self._prefixes}"
            )
        try:
            return self._resolve(path)
        except KeyError:
            return default

    def __getitem__(self, path: str) -> Any:
        value = self.get(path, _MISSING)
        if value is _MISSING:
            raise KeyError(path)
        return value


_MISSING = object()


@dataclass
class SkillContext:
    """技能执行上下文（引擎构造，全类型化，无裸 dict 业务字段）。"""

    request_id: str
    domain: str
    domain_input: DomainInput
    data_classification: str = "internal"
    preprocessed: List[PreprocessedItem] = field(default_factory=list)
    upstream_outputs: Dict[str, BaseModel] = field(default_factory=dict)
    state_view: Optional[StateView] = None
    error_rules: List[ErrorRule] = field(default_factory=list)
    available_tools: List[str] = field(default_factory=list)
    loop_count: int = 0
    clarifications: Dict[str, Any] = field(default_factory=dict)
    # 运行期配置读取（V2.1 P1-3）：只读、灰度分桶稳定、版本随快照固化
    config: Optional[Any] = None

    def anomalies_text(self) -> List[str]:
        out: List[str] = []
        for p in self.preprocessed:
            for a in p.anomalies:
                out.append(f"[{a.level.value}] {a.source}: {a.detail}")
        return out
