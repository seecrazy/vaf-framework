"""结构化事件流：JSONL 输出 + 可插拔 sink，统一经过脱敏钩子。"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Dict, List, Optional

from ..security.redaction import Redactor, default_redactor

EventSink = Callable[[Dict[str, object]], None]


class EventBus:
    def __init__(
        self,
        sinks: Optional[List[EventSink]] = None,
        redactor: Optional[Redactor] = None,
    ):
        self._sinks = list(sinks or [])
        self.redactor = redactor or default_redactor()
        self._lock = threading.Lock()
        self._buffer: List[Dict[str, object]] = []

    def publish(self, event_type: str, payload: Optional[Dict[str, object]] = None) -> None:
        event = {
            "ts": round(time.time(), 6),
            "type": event_type,
            "payload": dict(payload or {}),
        }
        with self._lock:
            self._buffer.append(event)
        for sink in self._sinks:
            sink(event)

    def tail(self, limit: int = 100) -> List[Dict[str, object]]:
        with self._lock:
            return list(self._buffer[-limit:])

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)


def jsonl_file_sink(path: str, redactor: Optional[Redactor] = None) -> EventSink:
    red = redactor or default_redactor()
    lock = threading.Lock()

    def sink(event: Dict[str, object]) -> None:
        line = json.dumps(red.redact_object(event), ensure_ascii=False, default=str)
        with lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    return sink
