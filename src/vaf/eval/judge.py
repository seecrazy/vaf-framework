"""裁判模型治理（DESIGN §13 / B4）：语义评估前必须登记，校准达标，变更告警。"""
from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..errors import VafError


class JudgeRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    version: str
    calibration_n: int
    agreement: float  # 与人工标注的一致率 0-1
    registered_at: float = 0.0
    active: bool = True


class JudgeModel(Protocol):
    def score(self, output_text: str, case_reference: str) -> float: ...


class JudgeGovernance:
    """裁判治理：未登记不得评审；agreement < 阈值拒绝；judge 变更产生告警事件。"""

    def __init__(self, min_agreement: float = 0.8, min_calibration_n: int = 30):
        self.min_agreement = min_agreement
        self.min_calibration_n = min_calibration_n
        self._lock = threading.Lock()
        self._registry: Dict[str, JudgeRegistration] = {}
        self._active_key: Optional[str] = None
        self.warnings: List[str] = []

    def register(self, model: str, version: str, calibration_n: int, agreement: float) -> JudgeRegistration:
        if agreement < self.min_agreement:
            raise VafError(
                f"judge model '{model}@{version}' agreement {agreement} < required "
                f"{self.min_agreement}; collect more calibration samples"
            )
        if calibration_n < self.min_calibration_n:
            raise VafError(
                f"judge model '{model}@{version}' calibration sample {calibration_n} < "
                f"required {self.min_calibration_n}"
            )
        import time

        reg = JudgeRegistration(
            model=model,
            version=version,
            calibration_n=calibration_n,
            agreement=agreement,
            registered_at=time.time(),
        )
        with self._lock:
            previous = self._registry.get(self._active_key) if self._active_key else None
            if previous is not None and (previous.model, previous.version) != (model, version):
                self.warnings.append(
                    f"judge model changed: {previous.model}@{previous.version} -> "
                    f"{model}@{version}; historical semantic scores are not comparable"
                )
            self._registry[f"{model}@{version}"] = reg
            self._active_key = f"{model}@{version}"
        return reg

    def require_registered(self) -> JudgeRegistration:
        with self._lock:
            if self._active_key is None:
                raise VafError(
                    "no judge model registered; semantic evaluation is forbidden until a "
                    "calibrated judge is registered"
                )
            return self._registry[self._active_key]

    def active(self) -> Optional[JudgeRegistration]:
        with self._lock:
            if self._active_key is None:
                return None
            return self._registry[self._active_key]
