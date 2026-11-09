"""分级脱敏（DESIGN §10.2）：数据分级 → 规则集。

- sensitive / confidential：PII 正则在进入 prompt、日志、事件前打码；
- internal：原文放行（访问留痕由审计层负责）；
- public：原文放行。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Pattern

CLASSIFICATIONS = ("public", "internal", "sensitive", "confidential")

# 敏感级别阈值：级别序号 >= 该值时启用脱敏
SENSITIVITY_ORDER = {name: idx for idx, name in enumerate(CLASSIFICATIONS)}
REDACT_FROM = "sensitive"

_DEFAULT_PATTERNS: List[tuple] = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone_cn", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("id_card_cn", re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")),
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
]


def _mask(text: str, match: "re.Match[str]") -> str:
    s = match.group(0)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:3] + "*" * (len(s) - 6) + s[-3:]


class Redactor:
    def __init__(self, patterns: Optional[List[tuple]] = None, redact_from: str = REDACT_FROM):
        self.patterns = list(patterns or _DEFAULT_PATTERNS)
        self.redact_from = redact_from

    def needs_redaction(self, classification: str) -> bool:
        if classification not in SENSITIVITY_ORDER:
            return False
        return SENSITIVITY_ORDER[classification] >= SENSITIVITY_ORDER[self.redact_from]

    def apply(self, text: str, classification: str) -> str:
        if not self.needs_redaction(classification):
            return text
        for _, pattern in self.patterns:
            text = pattern.sub(lambda m: _mask(text, m), text)
        return text

    def redact_object(self, obj: object, classification: str = "sensitive") -> object:
        """递归对 dict/list/str 脱敏（事件与日志出口统一调用）。"""
        return self._walk(obj, classification)

    def _walk(self, obj: object, classification: str) -> object:
        if isinstance(obj, str):
            return self.apply(obj, classification)
        if isinstance(obj, dict):
            return {k: self._walk(v, classification) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return type(obj)(self._walk(v, classification) for v in obj)
        return obj


_default: Optional[Redactor] = None


def default_redactor() -> Redactor:
    global _default
    if _default is None:
        _default = Redactor()
    return _default
