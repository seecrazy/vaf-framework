"""通知中心（DESIGN §7.13 / V2.1 P2-2）：审核/澄清/证伪等事件的推送出口。

- 订阅 EventBus，按事件类型分发给 sinks；
- webhook_sink：urllib POST JSON（超时 + 重试），支持共享密钥签名头；
- 失败只记指标与告警事件，绝不阻塞引擎主流程。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.request
from typing import Callable, Dict, List, Optional

from ..observe.metrics import default_metrics

NOTIFY_EVENT_TYPES = (
    "review_requested",
    "review_timeout",
    "clarification_requested",
    "falsification_triggered",
    "run_finished",
)

Sink = Callable[[str, Dict[str, object]], None]

# 内网直连 opener：显式绕过系统代理（macOS/企业环境常见拦截源）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post_json(
    url: str,
    payload: bytes,
    *,
    timeout_s: float = 3.0,
    retries: int = 2,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, str]] = None,
    on_success: Optional[Callable[[], None]] = None,
    on_failure: Optional[Callable[[Exception], None]] = None,
) -> bool:
    """带重试的 JSON POST（webhook / 消息卡片共用）。返回是否成功。"""
    if params:
        from urllib.parse import urlencode

        url = f"{url}?{urlencode(params)}"
    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers or {})
    last_error: Optional[Exception] = None
    for _attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers=req_headers, method="POST")
            with _opener.open(req, timeout=timeout_s) as resp:
                if 200 <= resp.status < 300:
                    if on_success is not None:
                        on_success()
                    return True
                raise RuntimeError(f"HTTP status {resp.status}")
        except Exception as exc:  # noqa: BLE001 - 推送失败不阻塞主流程
            last_error = exc
    if on_failure is not None:
        on_failure(last_error)
    return False


def webhook_sink(
    url: str,
    *,
    timeout_s: float = 3.0,
    retries: int = 2,
    secret: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Sink:
    """HTTP POST JSON sink；secret 提供时附 X-VAF-Signature（HMAC-SHA256 of body）。"""
    metrics = default_metrics()
    delivered = metrics.counter("webhook_delivered_total", "webhook 成功投递")
    failed = metrics.counter("webhook_failed_total", "webhook 投递失败")

    def sink(event_type: str, payload: Dict[str, object]) -> None:
        body = json.dumps(
            {"event": event_type, "payload": payload}, ensure_ascii=False, default=str
        ).encode("utf-8")
        req_headers = dict(headers or {})
        if secret:
            req_headers["X-VAF-Signature"] = hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
        post_json(
            url,
            body,
            timeout_s=timeout_s,
            retries=retries,
            headers=req_headers,
            on_success=lambda: delivered.inc(),
            on_failure=lambda exc: failed.inc(url=url, error=str(exc)[:80]),
        )

    return sink


class NotificationHub:
    """事件 → sinks 分发；可挂到 EventBus（attach）自动订阅关键事件。

    V2.10：默认异步投递（后台单线程 + 有界队列）——外部 webhook/IM 服务的
    网络状况不再阻塞引擎线程；队列满时丢弃并计数（保 run 不卡）。
    同步语义仍可用（async_delivery=False，测试/调试）。
    """

    def __init__(self, sinks: Optional[List[Sink]] = None, event_types=None,
                 async_delivery: bool = True, queue_size: int = 1000):
        self._sinks: List[Sink] = list(sinks or [])
        self.event_types = set(event_types or NOTIFY_EVENT_TYPES)
        self._lock = threading.Lock()
        self._metrics = default_metrics()
        self._sent = self._metrics.counter("notifications_sent_total", "通知发送次数")
        self._dropped = self._metrics.counter("notifications_failed_total", "通知失败次数")
        self._overflow = self._metrics.counter("notification_queue_dropped_total", "通知队列溢出丢弃")
        self._async = async_delivery
        import queue as _q
        self._queue: "_q.Queue" = _q.Queue(maxsize=queue_size)
        self._busy = threading.Event()  # worker 正在投递
        self._worker: Optional[threading.Thread] = None
        if async_delivery:
            self._worker = threading.Thread(
                target=self._drain_loop, name="vaf-notifier", daemon=True
            )
            self._worker.start()

    def add_sink(self, sink: Sink) -> None:
        with self._lock:
            self._sinks.append(sink)

    def notify(self, event_type: str, payload: Dict[str, object]) -> None:
        if event_type not in self.event_types:
            return
        if self._async:
            try:
                self._queue.put_nowait((event_type, payload))
            except Exception:  # noqa: BLE001 - 队列满：保 run 不卡
                self._overflow.inc()
            return
        self._dispatch(event_type, payload)

    def flush(self, timeout_s: float = 5.0) -> bool:
        """等待队列清空（优雅停机/测试用）。"""
        if not self._async:
            return True
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._queue.empty() and not self._busy.is_set():
                return True
            time.sleep(0.01)
        return False

    def _drain_loop(self) -> None:
        import queue as _q

        while True:
            try:
                event_type, payload = self._queue.get(timeout=1.0)
            except _q.Empty:
                continue
            self._busy.set()
            try:
                self._dispatch(event_type, payload)
            finally:
                self._busy.clear()

    def _dispatch(self, event_type: str, payload: Dict[str, object]) -> None:
        with self._lock:
            sinks = list(self._sinks)
        for sink in sinks:
            try:
                sink(event_type, payload)
                self._sent.inc()
            except Exception:  # noqa: BLE001
                self._dropped.inc()

    def attach(self, event_bus) -> None:
        """作为 EventBus sink 挂接（事件结构见 observe.events.EventBus.publish）。"""

        def _on_event(event: Dict[str, object]) -> None:
            self.notify(str(event.get("type", "")), dict(event.get("payload") or {}))  # type: ignore[arg-type]

        event_bus.add_sink(_on_event)
