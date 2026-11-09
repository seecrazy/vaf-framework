"""错误记忆（DESIGN §6.5 / B8）：规则生命周期 candidate→active→retired。

- Schema 类错误：自动激活（confidence 起始 0.7），命中递增；
- 业务类错误：人工确认后才激活；
- 容量上限淘汰 confidence×hits 最低者；过期自动退役；注入前仅取 active。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..observe.metrics import default_metrics


class ErrorRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    kind: str = "schema"  # schema | business
    scope: str = ""  # skill name or domain name
    pattern: str = ""
    rule_text: str
    status: str = "candidate"  # candidate | active | retired
    confidence: float = 0.7
    created_at: float = Field(default_factory=time.time)
    expires_at: Optional[float] = None
    hits: int = 0


class ErrorMemory:
    """技能级/领域级两层生效：scope 为技能名或 'domain:<name>'。"""

    def __init__(self, max_active_per_scope: int = 20):
        self._rules: Dict[str, ErrorRule] = {}
        self._lock = threading.Lock()
        self.max_active_per_scope = max_active_per_scope
        self._metrics = default_metrics()
        self._added = self._metrics.counter("error_rules_added", "错误规则新增")
        self._activated = self._metrics.counter("error_rules_activated", "错误规则激活")

    # ------------------------------------------------------------------
    def add(self, rule: ErrorRule) -> ErrorRule:
        if rule.kind == "schema":
            rule.status = "active"  # Schema 类自动激活
            rule.confidence = max(rule.confidence, 0.7)
        with self._lock:
            # 冲突处理：同 (kind, scope, pattern) 后到覆盖（B8）
            for existing in list(self._rules.values()):
                if (
                    existing.kind == rule.kind
                    and existing.scope == rule.scope
                    and existing.pattern == rule.pattern
                ):
                    rule.rule_id = existing.rule_id
            self._rules[rule.rule_id] = rule
            self._evict_if_needed(rule.scope)
        self._added.inc(kind=rule.kind)
        if rule.status == "active":
            self._activated.inc(kind=rule.kind)
        return rule

    def add_schema_error(self, scope: str, pattern: str, rule_text: str) -> ErrorRule:
        return self.add(
            ErrorRule(kind="schema", scope=scope, pattern=pattern, rule_text=rule_text)
        )

    def add_business_error(self, scope: str, pattern: str, rule_text: str) -> ErrorRule:
        """业务类错误：candidate，等待人工确认。"""
        return self.add(
            ErrorRule(
                kind="business",
                scope=scope,
                pattern=pattern,
                rule_text=rule_text,
                status="candidate",
                confidence=0.5,
            )
        )

    def activate(self, rule_id: str, approver: str = "") -> ErrorRule:
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None:
                raise KeyError(f"unknown error rule: {rule_id}")
            rule.status = "active"
            rule.hits += 0
        self._activated.inc(kind=rule.kind, approver=approver or "manual")
        return rule

    def retire(self, rule_id: str) -> ErrorRule:
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None:
                raise KeyError(f"unknown error rule: {rule_id}")
            rule.status = "retired"
        return rule

    def record_hit(self, rule_id: str) -> None:
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule is not None:
                rule.hits += 1

    # ------------------------------------------------------------------
    def rules_for(self, skill_name: str, domain: str = "", now: Optional[float] = None) -> List[ErrorRule]:
        """取注入 prompt 的 active 规则：技能级 + 领域级，过滤过期。"""
        now = now if now is not None else time.time()
        domain_scope = f"domain:{domain}" if domain else ""
        with self._lock:
            out = []
            for rule in self._rules.values():
                if rule.status != "active":
                    continue
                if rule.scope not in (skill_name, domain_scope):
                    continue
                if rule.expires_at is not None and rule.expires_at < now:
                    rule.status = "retired"
                    continue
                out.append(rule)
        out.sort(key=lambda r: (-(r.confidence * (1 + r.hits)), r.created_at))
        return out

    def all_rules(self, status: Optional[str] = None) -> List[ErrorRule]:
        with self._lock:
            rules = list(self._rules.values())
        if status:
            rules = [r for r in rules if r.status == status]
        return sorted(rules, key=lambda r: r.created_at)

    def _evict_if_needed(self, scope: str) -> None:
        active = [
            r for r in self._rules.values()
            if r.scope == scope and r.status == "active"
        ]
        if len(active) <= self.max_active_per_scope:
            return
        active.sort(key=lambda r: r.confidence * (1 + r.hits))
        for victim in active[: len(active) - self.max_active_per_scope]:
            victim.status = "retired"
