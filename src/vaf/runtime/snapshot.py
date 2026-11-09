"""执行计划快照（DESIGN §7.10 / A3）：run 与全版本组合绑定，杜绝断点续跑版本漂移。"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict

FRAMEWORK_VERSION = "2.4.0"


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_hash: str = ""
    framework_version: str = FRAMEWORK_VERSION
    pipeline_name: str = ""
    pipeline_version: str = ""
    skill_versions: Dict[str, str] = {}
    prompt_versions: Dict[str, int] = {}
    config_versions: Dict[str, int] = {}
    model_route_digest: str = ""

    def compute_hash(self) -> str:
        payload = {
            "framework": self.framework_version,
            "pipeline": [self.pipeline_name, self.pipeline_version],
            "skills": self.skill_versions,
            "prompts": self.prompt_versions,
            "config": self.config_versions,
            "model_routes": self.model_route_digest,
        }
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        self.plan_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return self.plan_hash


def build_plan(
    *,
    pipeline_name: str,
    pipeline_version: str,
    skill_versions: Dict[str, str],
    prompt_versions: Dict[str, int],
    config_versions: Dict[str, int],
    model_route_digest: str,
) -> ExecutionPlan:
    plan = ExecutionPlan(
        pipeline_name=pipeline_name,
        pipeline_version=pipeline_version,
        skill_versions=dict(skill_versions),
        prompt_versions=dict(prompt_versions),
        config_versions=dict(config_versions),
        model_route_digest=model_route_digest,
    )
    plan.compute_hash()
    return plan
