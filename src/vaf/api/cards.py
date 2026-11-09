"""消息卡片（V2.5 移动接入层）：把审核/澄清事件推成钉钉/企微/飞书可操作卡片。

卡片按钮/H5 链接指向网关的移动审核台（`/`），点开即批——审核员无需安装任何 App。
平台格式均为各自群机器人官方协议：
- 钉钉自定义机器人：actionCard + 加签（timestamp 毫秒 + HMAC-SHA256 base64 查询参数）；
- 企业微信群机器人：markdown 消息；
- 飞书自定义机器人：interactive 卡片 + 签名（timestamp 秒 + HMAC-SHA256 base64 放 body）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

from ..observe.metrics import default_metrics
from ..runtime.notification import post_json

CARD_PLATFORMS = ("dingtalk", "wecom", "feishu")


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------
def _hmac_b64(secret: str, data: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).digest()
    ).decode()


def dingtalk_sign(secret: str, timestamp_ms: int) -> str:
    return _hmac_b64(secret, f"{timestamp_ms}\n{secret}")


def feishu_sign(secret: str, timestamp_s: int) -> str:
    return _hmac_b64(secret, f"{timestamp_s}\n{secret}")


# ---------------------------------------------------------------------------
# 文案与链接
# ---------------------------------------------------------------------------
def _review_url(base: str, task_id: str) -> str:
    return f"{base.rstrip('/')}/#/task/{task_id}"


def _clarify_url(base: str, request_id: str) -> str:
    return f"{base.rstrip('/')}/#/run/{request_id}/clarify"


def _review_lines(payload: Dict[str, object]) -> str:
    digest = payload.get("payload_digest") or {}
    lines = [
        f"**run**：{payload.get('request_id', '')}",
        f"**pipeline**：{payload.get('pipeline', '')} / 步骤 {payload.get('step', '')}",
    ]
    for k, v in digest.items():
        text = str(v)
        lines.append(f"- {k}：{text[:60]}{'…' if len(text) > 60 else ''}")
    if not digest:
        lines.append(f"- 审核字段：{', '.join(payload.get('fields') or []) or '（未声明）'}")
    if payload.get("required_role"):
        lines.append(f"- 需要角色：{payload['required_role']}")
    return "\n\n".join(lines)


def _clarify_lines(payload: Dict[str, object]) -> str:
    questions = payload.get("questions") or []
    lines = [
        f"**run**：{payload.get('request_id', '')}（第 {payload.get('round_no', '?')} 轮）",
        f"**pipeline**：{payload.get('pipeline', '')} / 步骤 {payload.get('step', '')}",
        "",
    ]
    lines += [f"{i}. {q}" for i, q in enumerate(questions, start=1)]
    return "\n".join(lines)


def _card_text(event_type: str, payload: Dict[str, object]) -> tuple:
    if event_type == "clarification_requested":
        return ("❓ 待补充信息", _clarify_lines(payload), "clarify")
    if event_type == "review_timeout":
        return ("⏰ 审核超时", f"run {payload.get('request_id', '')} 步骤 {payload.get('step', '')} 超时，动作 {payload.get('action', '')}", None)
    return ("🔴 待人工审核", _review_lines(payload), "review")


# ---------------------------------------------------------------------------
# 各平台 payload 构建（纯函数，便于测试）
# ---------------------------------------------------------------------------
def dingtalk_payload(event_type: str, payload: Dict[str, object], review_base: str) -> Dict[str, Any]:
    title, text, kind = _card_text(event_type, payload)
    card: Dict[str, Any] = {"title": title, "text": f"### {title}\n\n{text}", "btnOrientation": "0"}
    if kind == "review":
        card.update(singleTitle="去处理", singleURL=_review_url(review_base, str(payload.get("task_id", ""))))
    elif kind == "clarify":
        card.update(singleTitle="去回答", singleURL=_clarify_url(review_base, str(payload.get("request_id", ""))))
    else:
        card.update(singleTitle="查看详情", singleURL=review_base.rstrip("/"))
    return {"msgtype": "actionCard", "actionCard": card}


def wecom_payload(event_type: str, payload: Dict[str, object], review_base: str) -> Dict[str, Any]:
    title, text, kind = _card_text(event_type, payload)
    if kind == "review":
        link = _review_url(review_base, str(payload.get("task_id", "")))
        action = f"[点击去处理]({link})"
    elif kind == "clarify":
        link = _clarify_url(review_base, str(payload.get("request_id", "")))
        action = f"[点击去回答]({link})"
    else:
        action = ""
    content = f"**{title}**\n{text}"
    if action:
        content += f"\n\n{action}"
    return {"msgtype": "markdown", "markdown": {"content": content}}


def feishu_payload(event_type: str, payload: Dict[str, object], review_base: str) -> Dict[str, Any]:
    title, text, kind = _card_text(event_type, payload)
    button = {"tag": "button", "text": {"tag": "plain_text", "content": "查看详情"}, "type": "default", "url": review_base.rstrip("/")}
    if kind == "review":
        button.update(text={"tag": "plain_text", "content": "去处理"}, type="primary",
                      url=_review_url(review_base, str(payload.get("task_id", ""))))
    elif kind == "clarify":
        button.update(text={"tag": "plain_text", "content": "去回答"}, type="primary",
                      url=_clarify_url(review_base, str(payload.get("request_id", ""))))
    return {
        "msgtype": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "red" if kind == "review" else "orange"},
            "elements": [
                {"tag": "markdown", "content": text},
                {"tag": "hr"},
                {"tag": "action", "actions": [button]},
            ],
        },
    }


# ---------------------------------------------------------------------------
# sink 工厂（挂到 NotificationHub）
# ---------------------------------------------------------------------------
def card_sink(
    platform: str,
    url: str,
    *,
    review_base: str,
    secret: Optional[str] = None,
    timeout_s: float = 3.0,
    retries: int = 1,
):
    """平台消息卡片 sink：platform ∈ dingtalk/wecom/feishu。"""
    if platform not in CARD_PLATFORMS:
        raise ValueError(f"不支持的平台 {platform!r}；可用：{CARD_PLATFORMS}")
    builder = {"dingtalk": dingtalk_payload, "wecom": wecom_payload, "feishu": feishu_payload}[platform]
    metrics = default_metrics()
    ok = metrics.counter("card_delivered_total", "卡片成功投递")
    bad = metrics.counter("card_failed_total", "卡片投递失败")

    def sink(event_type: str, payload: Dict[str, object]) -> None:
        if event_type not in ("review_requested", "clarification_requested", "review_timeout"):
            return
        body = builder(event_type, payload, review_base)
        params = None
        if platform == "dingtalk" and secret:
            ts = int(time.time() * 1000)
            params = {"timestamp": str(ts), "sign": dingtalk_sign(secret, ts)}
        if platform == "feishu" and secret:
            ts = int(time.time())
            body.update({"timestamp": str(ts), "sign": feishu_sign(secret, ts)})
        post_json(
            url,
            json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"),
            timeout_s=timeout_s,
            retries=retries,
            params=params,
            on_success=lambda: ok.inc(platform=platform),
            on_failure=lambda exc: bad.inc(platform=platform, error=str(exc)[:80]),
        )

    return sink
