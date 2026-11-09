"""V2.5 移动接入层：HTTP 网关 + 消息卡片 + H5 审核台。

依赖（可选安装）：pip install "vaf[api]"
"""
from .auth import ApiKeyStore
from .cards import card_sink, dingtalk_payload, feishu_payload, wecom_payload, CARD_PLATFORMS
from .gateway import create_gateway

__all__ = [
    "ApiKeyStore",
    "card_sink", "dingtalk_payload", "feishu_payload", "wecom_payload", "CARD_PLATFORMS",
    "create_gateway",
]
