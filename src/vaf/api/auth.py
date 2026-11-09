"""API Key 鉴权（V2.5 移动接入层）：Key → RBAC 用户映射。

两种模式：
- 配置了 keys：所有请求必须带 X-VAF-API-Key，映射到 User 后走框架 RBAC；
- 未配置（开放模式）：合成 admin 用户——仅供内网/演示，文档明示。

Key 配置格式（CLI --api-key 可重复）："key:user_id:role1,role2"
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Dict, List, Optional

from ..errors import VafError
from ..security.permissions import User

_ADMIN = User("open-mode-admin", {"admin"})


class ApiKeyStore:
    def __init__(self, users_by_key: Dict[str, User]):
        self._users = dict(users_by_key)

    @classmethod
    def from_specs(cls, specs: List[str]) -> "ApiKeyStore":
        """解析 ["key:user:role1,role2", ...]。"""
        users: Dict[str, User] = {}
        for spec in specs:
            parts = spec.split(":", 2)
            if len(parts) != 3 or not all(parts):
                raise VafError(
                    f"API key 配置非法：{spec!r}（格式 key:user_id:role1,role2）"
                )
            key, user_id, roles = parts
            users[key] = User(user_id, {r.strip() for r in roles.split(",") if r.strip()})
        return cls(users)

    @classmethod
    def generate(cls, user_id: str, roles: List[str]) -> tuple:
        """生成一把随机 key（用于引导配置）。返回 (key, spec)。"""
        key = "vaf_" + secrets.token_urlsafe(24)
        spec = f"{key}:{user_id}:{','.join(roles)}"
        return key, spec

    def authenticate(self, api_key: str) -> Optional[User]:
        if not api_key or not self._users:
            return None
        user = self._users.get(api_key)
        return user

    @property
    def enabled(self) -> bool:
        return bool(self._users)
