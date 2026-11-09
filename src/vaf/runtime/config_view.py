"""运行期配置视图（DESIGN §14 / V2.1 P1-3）：技能在推理期读取配置中心。

- 只读；带 request_id 参与灰度分桶（同一 run 内读取稳定）；
- 读取的 key 版本已随执行计划快照固化（热更新只影响新 run，A3）。
"""
from __future__ import annotations

from typing import Any, Optional

from ..config.center import ConfigCenter


class ConfigView:
    def __init__(self, center: ConfigCenter, request_id: str, tenant: str = "default"):
        self._center = center
        self._request_id = request_id
        self._tenant = tenant

    def get(self, key: str, default: Any = None) -> Any:
        return self._center.get(
            key,
            default,
            tenant=self._tenant,
            request_id=self._request_id,
        )

    def __contains__(self, key: str) -> bool:
        sentinel = object()
        return self.get(key, sentinel) is not sentinel
