"""RBAC 权限模型（DESIGN §10.1）：User → Role → Permission。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, Optional, Set

from ..errors import PermissionDeniedError


class Permission(str, Enum):
    RUN_PIPELINE = "run_pipeline"
    SUBMIT_REVIEW = "submit_review"
    REVIEW_APPROVE = "review_approve"
    CONFIG_WRITE = "config_write"
    AUDIT_READ = "audit_read"
    DATA_EXPORT = "data_export"
    ADMIN = "admin"


@dataclass
class User:
    user_id: str
    roles: Set[str] = field(default_factory=set)


class RolePermissionMap:
    """角色 → 权限集合；admin 角色拥有全部权限。"""

    ALL = {p.value for p in Permission}

    def __init__(self) -> None:
        self._map: Dict[str, Set[str]] = {
            "admin": set(self.ALL),
            "operator": {Permission.RUN_PIPELINE.value, Permission.SUBMIT_REVIEW.value, Permission.AUDIT_READ.value},
            "reviewer": {Permission.SUBMIT_REVIEW.value, Permission.REVIEW_APPROVE.value, Permission.AUDIT_READ.value},
            "auditor": {Permission.AUDIT_READ.value, Permission.DATA_EXPORT.value},
            "config_admin": {Permission.CONFIG_WRITE.value},
        }

    def grant(self, role: str, permission: Permission | str) -> None:
        self._map.setdefault(role, set()).add(Permission(permission).value)

    def permissions_of(self, roles: Iterable[str]) -> Set[str]:
        out: Set[str] = set()
        for role in roles:
            out |= self._map.get(role, set())
        return out


class PermissionChecker:
    def __init__(self, role_map: Optional[RolePermissionMap] = None):
        self.role_map = role_map or RolePermissionMap()

    def has(self, user: Optional[User], permission: Permission) -> bool:
        if user is None:
            return False
        return permission.value in self.role_map.permissions_of(user.roles)

    def require(self, user: Optional[User], permission: Permission, context: str = "") -> None:
        if not self.has(user, permission):
            who = user.user_id if user else "<anonymous>"
            raise PermissionDeniedError(
                f"user '{who}' lacks permission '{permission.value}'"
                + (f" (context: {context})" if context else "")
            )

    def require_any_role(self, user: Optional[User], roles: Iterable[str]) -> None:
        if user is None or not (set(user.roles) & set(roles)):
            who = user.user_id if user else "<anonymous>"
            raise PermissionDeniedError(f"user '{who}' lacks any of roles {sorted(roles)}")
