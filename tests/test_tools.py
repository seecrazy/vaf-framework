"""工具适配层测试：白名单 / RBAC / 幂等缓存 / 超时 / 审计。"""
import time

import pytest

from vaf.errors import PermissionDeniedError, ToolError, ToolTimeoutError
from vaf.llm.client import ToolCallRequest
from vaf.security.audit import AuditLog, MemoryAuditSink
from vaf.security.permissions import Permission, PermissionChecker, User
from vaf.tools.adapter import ToolAdapter, ToolContext
from vaf.tools.registry import ToolRegistry, ToolSpec


def _adapter(**kw):
    registry = ToolRegistry()
    sink = MemoryAuditSink()
    audit = AuditLog([sink])
    adapter = ToolAdapter(registry, permission_checker=kw.get("perms", PermissionChecker()), audit=audit)
    return registry, adapter, sink


def _ctx(skill="s1", tools=None, user=None):
    return ToolContext("req-1", skill, tools if tools is not None else ["echo"], user)


def test_whitelist_rejects_undeclared_tool():
    registry, adapter, _ = _adapter()
    registry.register(lambda a: {"ok": True}, ToolSpec(name="echo"))
    ctx = _ctx(tools=[])  # 技能未声明任何工具
    with pytest.raises(PermissionDeniedError, match="not declared"):
        adapter.call(ToolCallRequest(call_id="1", name="echo", arguments={}), ctx)


def test_required_permission_enforced():
    registry, adapter, _ = _adapter()
    registry.register(
        lambda a: {"ok": True},
        ToolSpec(name="danger", required_permission=Permission.ADMIN.value),
    )
    ctx = _ctx(tools=["danger"], user=User("u", {"operator"}))
    with pytest.raises(PermissionDeniedError, match="admin"):
        adapter.call(ToolCallRequest(call_id="1", name="danger", arguments={}), ctx)
    ok = adapter.call(
        ToolCallRequest(call_id="2", name="danger", arguments={}),
        _ctx(tools=["danger"], user=User("admin", {"admin"})),
    )
    assert ok == {"ok": True}


def test_idempotent_cache_for_side_effect_tools():
    registry, adapter, sink = _adapter()
    calls = {"n": 0}

    def slow_side_effect(args):
        calls["n"] += 1
        return {"ticket": calls["n"]}

    registry.register(
        slow_side_effect,
        ToolSpec(name="create_ticket", has_side_effect=True, idempotent=True),
    )
    ctx = _ctx(tools=["create_ticket"])
    r1 = adapter.call(ToolCallRequest(call_id="1", name="create_ticket", arguments={"x": 1}), ctx)
    r2 = adapter.call(ToolCallRequest(call_id="2", name="create_ticket", arguments={"x": 1}), ctx)
    assert r1 == r2 and calls["n"] == 1  # 第二次命中幂等缓存


def test_timeout_enforced():
    registry, adapter, _ = _adapter()
    registry.register(
        lambda a: (time.sleep(0.5), {"late": True})[1],
        ToolSpec(name="slow", timeout_s=0.05),
    )
    with pytest.raises(ToolTimeoutError, match="timed out"):
        adapter.call(ToolCallRequest(call_id="1", name="slow", arguments={}), _ctx(tools=["slow"]))


def test_tool_error_wrapped_and_audited():
    registry, adapter, sink = _adapter()

    def boom(args):
        raise ValueError("inner")

    registry.register(boom, ToolSpec(name="boom"))
    with pytest.raises(ToolError, match="failed: inner"):
        adapter.call(ToolCallRequest(call_id="1", name="boom", arguments={}), _ctx(tools=["boom"]))
    records = sink.records("tool_call")
    assert any(r.detail["status"] == "error" for r in records)


def test_unknown_tool():
    registry, adapter, _ = _adapter()
    with pytest.raises(ToolError, match="unknown tool"):
        adapter.call(ToolCallRequest(call_id="1", name="ghost", arguments={}), _ctx(tools=["ghost"]))


def test_definitions_respect_allowlist():
    registry, _, _ = _adapter()
    registry.register(lambda a: {}, ToolSpec(name="a"))
    registry.register(lambda a: {}, ToolSpec(name="b"))
    defs = registry.definitions(allowed=["a"])
    assert [d["function"]["name"] for d in defs] == ["a"]
