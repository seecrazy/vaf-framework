"""安全层测试：分级脱敏 / RBAC / 审计。"""
import pytest

from vaf.errors import PermissionDeniedError
from vaf.security.audit import AuditLog, JsonlAuditSink, MemoryAuditSink
from vaf.security.permissions import Permission, PermissionChecker, RolePermissionMap, User
from vaf.security.redaction import Redactor


# ---------------------------------------------------------------- 脱敏
def test_redaction_applies_by_classification():
    red = Redactor()
    text = "联系人 zhang@example.com 电话 13800138000"
    assert "13800138000" not in red.apply(text, "sensitive")
    assert "zhang@example.com" not in red.apply(text, "confidential")
    # internal/public 不脱敏
    assert "13800138000" in red.apply(text, "internal")
    assert "13800138000" in red.apply(text, "public")


def test_redaction_masks_preserve_ends():
    red = Redactor()
    masked = red.apply("卡号 6222020200112233", "confidential")
    assert "6222020200112233" not in masked
    assert "*" in masked


def test_redact_object_recursive():
    red = Redactor()
    obj = {"a": ["手机 13800138000"], "b": {"c": "x@y.com"}, "d": 42}
    out = red.redact_object(obj, "sensitive")
    assert "13800138000" not in out["a"][0]
    assert "x@y.com" not in out["b"]["c"]
    assert out["d"] == 42


def test_needs_redaction_threshold():
    red = Redactor()
    assert not red.needs_redaction("internal")
    assert red.needs_redaction("sensitive")
    assert red.needs_redaction("confidential")


# ---------------------------------------------------------------- RBAC
def test_role_permissions_and_require():
    checker = PermissionChecker()
    admin = User("root", {"admin"})
    assert checker.has(admin, Permission.CONFIG_WRITE)
    checker.require(admin, Permission.CONFIG_WRITE)  # 不抛

    operator = User("op", {"operator"})
    assert checker.has(operator, Permission.RUN_PIPELINE)
    with pytest.raises(PermissionDeniedError):
        checker.require(operator, Permission.CONFIG_WRITE)


def test_anonymous_has_no_permission():
    checker = PermissionChecker()
    assert not checker.has(None, Permission.RUN_PIPELINE)
    with pytest.raises(PermissionDeniedError, match="anonymous"):
        checker.require(None, Permission.RUN_PIPELINE)


def test_grant_custom_role():
    role_map = RolePermissionMap()
    role_map.grant("intern", Permission.RUN_PIPELINE)
    checker = PermissionChecker(role_map)
    assert checker.has(User("i", {"intern"}), Permission.RUN_PIPELINE)
    assert not checker.has(User("i", {"intern"}), Permission.REVIEW_APPROVE)


def test_require_any_role():
    checker = PermissionChecker()
    checker.require_any_role(User("d", {"reviewer"}), ["reviewer"])
    with pytest.raises(PermissionDeniedError):
        checker.require_any_role(User("o", {"operator"}), ["reviewer"])


# ---------------------------------------------------------------- 审计
def test_audit_log_fanout_and_memory_query(tmp_path):
    sink = MemoryAuditSink()
    file_sink = JsonlAuditSink(str(tmp_path / "audit.jsonl"))
    log = AuditLog([sink, file_sink])
    log.record("alice", "review_decision", "step1", {"decision": "approved"}, "run-1")
    log.record("bob", "config_set", "key@prod")
    assert len(sink.records()) == 2
    assert len(sink.records("review_decision")) == 1
    content = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "review_decision" in content and "alice" in content
