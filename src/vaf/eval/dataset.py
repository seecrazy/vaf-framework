"""黄金数据集（DESIGN §13）：GoldenCase 定义与 YAML 加载。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    input: Dict[str, Any]
    expected_conclusion: Optional[str] = None
    expected_findings: List[str] = Field(default_factory=list)  # finding_id 列表
    tags: List[str] = Field(default_factory=list)


class GoldenDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    cases: List[GoldenCase]

    def filtered(self, tag: Optional[str] = None) -> List[GoldenCase]:
        if tag is None:
            return self.cases
        return [c for c in self.cases if tag in c.tags]


def dataset_from_dict(data: Dict[str, Any]) -> GoldenDataset:
    return GoldenDataset.model_validate(data)


def dataset_from_yaml(path: str) -> GoldenDataset:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"golden dataset must be a mapping: {path}")
    return dataset_from_dict(data)


def dataset_from_yaml_text(text: str) -> GoldenDataset:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("golden dataset must be a mapping")
    return dataset_from_dict(data)
