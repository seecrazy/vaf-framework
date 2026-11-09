"""配置中心（DESIGN §14）：版本链 / 多环境 / 热更新 / 灰度 / 回滚。

热更新只影响新 run：引擎在 run 发起时把 config_versions 固化进执行计划快照（A3）。
灰度规则：tenant_in（租户白名单）与 percentage（按 hash(request_id+key) 稳定分桶）。
"""
from __future__ import annotations

import hashlib
import threading
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..errors import VafError
from ..observe.metrics import default_metrics
from ..security.audit import AuditLog

ENVIRONMENTS = ("dev", "test", "staging", "prod")


class ConfigVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    value: Any
    checksum: str
    actor: str = ""
    ts: float = 0.0
    note: str = ""


class GrayRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    env: str
    version: int  # 灰度目标版本
    baseline_version: int  # 未命中灰度的流量使用的基线版本
    tenant_in: List[str] = Field(default_factory=list)
    percentage: float = 100.0  # 0-100；100 表示全量


class ConfigCenter:
    def __init__(self, env: str = "test", audit: Optional[AuditLog] = None):
        if env not in ENVIRONMENTS:
            raise VafError(f"unknown env '{env}', expected one of {ENVIRONMENTS}")
        self.env = env
        self.audit = audit
        # (key, env) → 版本链
        self._versions: Dict[tuple, List[ConfigVersion]] = {}
        self._gray: Dict[tuple, GrayRule] = {}
        self._listeners: Dict[str, List[Callable[[str, Any], None]]] = {}
        self._lock = threading.RLock()
        self._metrics = default_metrics()
        self._changes = self._metrics.counter("config_changes_total", "配置变更次数")

    # ------------------------------------------------------------------
    def set(
        self,
        key: str,
        value: Any,
        *,
        env: Optional[str] = None,
        actor: str = "system",
        note: str = "",
    ) -> int:
        target_env = env or self.env
        with self._lock:
            chain = self._versions.setdefault((key, target_env), [])
            version = (chain[-1].version + 1) if chain else 1
            record = ConfigVersion(
                version=version,
                value=value,
                checksum=_checksum(value),
                actor=actor,
                ts=__import__("time").time(),
                note=note,
            )
            chain.append(record)
            # 同 key 新版本写入后，原灰度规则失效（显式重新配置）
            self._gray.pop((key, target_env), None)
        self._changes.inc(key=key, env=target_env)
        if self.audit:
            self.audit.record(
                actor=actor,
                action="config_set",
                target=f"{key}@{target_env}",
                detail={"version": version, "checksum": record.checksum, "note": note},
            )
        for listener in self._listeners.get(key, []):  # 热更新回调
            listener(key, value)
        return version

    def get(
        self,
        key: str,
        default: Any = None,
        *,
        env: Optional[str] = None,
        tenant: str = "default",
        request_id: str = "",
    ) -> Any:
        target_env = env or self.env
        with self._lock:
            chain = self._versions.get((key, target_env)) or []
            if not chain:
                return default
            gray = self._gray.get((key, target_env))
            if gray is not None:
                value_by_version = {r.version: r.value for r in chain}
                if tenant in gray.tenant_in or _bucket(request_id or tenant, key) < gray.percentage:
                    if gray.version in value_by_version:
                        return value_by_version[gray.version]
                baseline = value_by_version.get(gray.baseline_version)
                if baseline is not None:
                    return baseline  # 未命中灰度 → 基线版本
            return chain[-1].value

    def set_gray(
        self,
        key: str,
        version: int,
        *,
        tenant_in: Optional[List[str]] = None,
        percentage: float = 100.0,
        env: Optional[str] = None,
        actor: str = "system",
    ) -> GrayRule:
        target_env = env or self.env
        with self._lock:
            chain = self._versions.get((key, target_env)) or []
            if not any(r.version == version for r in chain):
                raise VafError(f"cannot gray-release unknown version {version} of '{key}'")
            # 基线 = 灰度目标版本之前的最近版本
            older = [r.version for r in chain if r.version < version]
            baseline = max(older) if older else version
            rule = GrayRule(
                key=key,
                env=target_env,
                version=version,
                baseline_version=baseline,
                tenant_in=list(tenant_in or []),
                percentage=max(0.0, min(100.0, percentage)),
            )
            self._gray[(key, target_env)] = rule
        if self.audit:
            self.audit.record(
                actor=actor,
                action="config_gray",
                target=f"{key}@{target_env}",
                detail={"version": version, "percentage": percentage, "tenant_in": rule.tenant_in},
            )
        return rule

    def rollback(self, key: str, to_version: int, *, env: Optional[str] = None, actor: str = "system") -> int:
        """回滚 = 把历史版本的值写为新版本（版本链只增不减，全量留痕）。"""
        target_env = env or self.env
        with self._lock:
            chain = self._versions.get((key, target_env)) or []
            record = next((r for r in chain if r.version == to_version), None)
            if record is None:
                raise VafError(f"cannot rollback '{key}' to unknown version {to_version}")
            value = record.value
        return self.set(key, value, env=target_env, actor=actor, note=f"rollback to v{to_version}")

    def history(self, key: str, *, env: Optional[str] = None) -> List[ConfigVersion]:
        target_env = env or self.env
        with self._lock:
            return list(self._versions.get((key, target_env), []))

    def versions_snapshot(self, env: Optional[str] = None) -> Dict[str, int]:
        """执行计划快照用：本环境全部 key 的当前版本号。"""
        target_env = env or self.env
        with self._lock:
            return {
                key: chain[-1].version
                for (key, e), chain in self._versions.items()
                if e == target_env and chain
            }

    def on_change(self, key: str, listener: Callable[[str, Any], None]) -> None:
        with self._lock:
            self._listeners.setdefault(key, []).append(listener)


def _checksum(value: Any) -> str:
    import json

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:12]


def _bucket(identity: str, key: str) -> float:
    digest = hashlib.sha256(f"{identity}:{key}".encode()).hexdigest()
    return (int(digest[:8], 16) % 10000) / 100.0
