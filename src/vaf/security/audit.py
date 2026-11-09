"""审计日志（DESIGN §10.3）：append-only，覆盖 run 生命周期 / 审核决策 / 配置变更 / 工具调用 / 证伪处置。"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional

from ..observe.metrics import default_metrics


class AuditRecord:
    """链式哈希审计记录：hash = SHA256(prev_hash + 本条内容)——篡改任何历史
    记录都会导致后续链断裂（append-only + 可验证）。"""

    __slots__ = ("ts", "actor", "action", "target", "detail", "request_id", "prev_hash", "hash")

    def __init__(self, actor: str, action: str, target: str, detail: Optional[Dict[str, object]] = None,
                 request_id: str = "", prev_hash: str = ""):
        self.ts = round(time.time(), 6)
        self.actor = actor
        self.action = action
        self.target = target
        self.detail = dict(detail or {})
        self.request_id = request_id
        self.prev_hash = prev_hash
        self.hash = self._compute_hash()

    def _compute_hash(self) -> str:
        import hashlib
        import json as _json

        payload = _json.dumps(
            [self.prev_hash, self.ts, self.actor, self.action, self.target,
             self.detail, self.request_id],
            sort_keys=True, ensure_ascii=False, default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, object]:
        return {
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
            "request_id": self.request_id,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


class AuditSink:
    def write(self, record: AuditRecord) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class MemoryAuditSink(AuditSink):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: List[AuditRecord] = []

    def write(self, record: AuditRecord) -> None:
        with self._lock:
            self._records.append(record)

    def records(self, action: Optional[str] = None) -> List[AuditRecord]:
        with self._lock:
            if action is None:
                return list(self._records)
            return [r for r in self._records if r.action == action]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


class JsonlAuditSink(AuditSink):
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, record: AuditRecord) -> None:
        line = json.dumps(record.to_dict(), ensure_ascii=False, default=str)
        with self._lock, open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class AuditLog:
    """多 sink 扇出 + 指标计数。禁止删除已有记录（append-only）。"""

    def __init__(self, sinks: Optional[List[AuditSink]] = None):
        self._sinks: List[AuditSink] = list(sinks or [])
        self._counter = default_metrics().counter("audit_records_total", "审计记录总数")
        self._last_hash = ""  # 链头

    def add_sink(self, sink: AuditSink) -> None:
        self._sinks.append(sink)

    def record(
        self,
        actor: str,
        action: str,
        target: str,
        detail: Optional[Dict[str, object]] = None,
        request_id: str = "",
    ) -> AuditRecord:
        rec = AuditRecord(actor, action, target, detail, request_id, prev_hash=self._last_hash)
        self._last_hash = rec.hash
        self._counter.inc(action=action)
        for sink in self._sinks:
            sink.write(rec)
        return rec
