"""工具适配器（DESIGN §15）：白名单 → RBAC → 幂等缓存 → 超时 → 审计/指标 → 补偿登记。"""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Dict, List, Optional

from ..errors import PermissionDeniedError, ToolError, ToolTimeoutError
from ..llm.client import ToolCallRequest
from ..observe.metrics import MetricsRegistry, default_metrics
from ..security.audit import AuditLog
from ..security.permissions import Permission, PermissionChecker, User
from .registry import ToolRegistry


class ToolContext:
    """一次工具调用的执行上下文（由技能执行器构造）。"""

    def __init__(
        self,
        request_id: str,
        skill_name: str,
        allowed_tools: List[str],
        user: Optional[User] = None,
        data_classification: str = "internal",
    ):
        self.request_id = request_id
        self.skill_name = skill_name
        self.allowed_tools = list(allowed_tools)
        self.user = user
        self.data_classification = data_classification


class ToolResult(dict):
    """工具返回（dict 子类），附执行元数据。"""


class ToolAdapter:
    def __init__(
        self,
        registry: ToolRegistry,
        permission_checker: Optional[PermissionChecker] = None,
        audit: Optional[AuditLog] = None,
        metrics: Optional[MetricsRegistry] = None,
        compensation_registry: Optional[Callable[[str, str, str, str], None]] = None,
    ):
        self.registry = registry
        self.permissions = permission_checker or PermissionChecker()
        self.audit = audit
        self._metrics = metrics or default_metrics()
        self._calls = self._metrics.counter("tool_calls_total", "工具调用次数")
        self._failures = self._metrics.counter("tool_failures_total", "工具失败次数")
        self._idempotent_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.Lock()
        self._undo_handlers: Dict[str, Callable[[], Dict[str, Any]]] = {}
        self._undo_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="vaf-tool")
        # compensation_registry(undo_key, request_id, tool_name, description)
        self._compensation_registry = compensation_registry

    # ------------------------------------------------------------------
    def call(self, call: ToolCallRequest, ctx: ToolContext) -> Dict[str, Any]:
        spec = self.registry.spec(call.name)
        # 1. 技能白名单（None=不受限；空列表=全部禁止——fail-closed）
        if ctx.allowed_tools is not None and call.name not in ctx.allowed_tools:
            self._failures.inc(tool=call.name, reason="not_whitelisted")
            raise PermissionDeniedError(
                f"tool '{call.name}' not declared by skill '{ctx.skill_name}'"
            )
        # 2. RBAC
        if spec.required_permission:
            self.permissions.require(ctx.user, Permission(spec.required_permission), context=f"tool:{call.name}")
        # 3. 幂等缓存（有副作用且幂等才缓存）
        cache_key = ""
        if spec.has_side_effect and spec.idempotent:
            cache_key = self._cache_key(ctx.request_id, call)
            with self._cache_lock:
                if cache_key in self._idempotent_cache:
                    self._calls.inc(tool=call.name, cache="hit")
                    return dict(self._idempotent_cache[cache_key])
        # 4. 补偿登记（有副作用且非幂等：执行前登记 undo 闭包）
        if spec.has_side_effect and not spec.idempotent:
            undo_key = f"{ctx.request_id}:{call.name}:{_args_hash(call.arguments)}"
            undo_fn = self.registry.undo_fn(call.name)
            if undo_fn is not None:
                with self._undo_lock:
                    self._undo_handlers[undo_key] = lambda: undo_fn(dict(call.arguments))
            if self._compensation_registry is not None:
                self._compensation_registry(undo_key, ctx.request_id, call.name, spec.description)
        # 5. 执行（超时控制）
        fn = self.registry.fn(call.name)
        self._calls.inc(tool=call.name)
        try:
            future = self._executor.submit(fn, dict(call.arguments))
            result = future.result(timeout=spec.timeout_s)
        except FutureTimeout as exc:
            self._failures.inc(tool=call.name, reason="timeout")
            self._record(ctx, call, "timeout")
            raise ToolTimeoutError(f"tool '{call.name}' timed out after {spec.timeout_s}s") from exc
        except ToolError:
            self._failures.inc(tool=call.name, reason="error")
            self._record(ctx, call, "error")
            raise
        except Exception as exc:  # noqa: BLE001
            self._failures.inc(tool=call.name, reason="error")
            self._record(ctx, call, "error")
            raise ToolError(f"tool '{call.name}' failed: {exc}") from exc
        self._record(ctx, call, "ok")
        if cache_key:
            with self._cache_lock:
                self._idempotent_cache[cache_key] = dict(result or {})
        return dict(result or {})

    def undo(self, undo_key: str) -> bool:
        """执行补偿：调用注册的 undo_fn；返回是否执行了补偿。"""
        with self._undo_lock:
            handler = self._undo_handlers.pop(undo_key, None)
        if handler is None:
            return False
        handler()
        if self.audit is not None:
            self.audit.record(
                actor="compensator", action="tool_undo", target=undo_key, detail={}
            )
        return True

    # ------------------------------------------------------------------
    def _cache_key(self, request_id: str, call: ToolCallRequest) -> str:
        return f"{request_id}:{call.name}:{_args_hash(call.arguments)}"

    def _record(self, ctx: ToolContext, call: ToolCallRequest, status: str) -> None:
        if self.audit is not None:
            self.audit.record(
                actor=ctx.skill_name,
                action="tool_call",
                target=call.name,
                detail={"status": status, "arguments": call.arguments},
                request_id=ctx.request_id,
            )


def _args_hash(arguments: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:16]
