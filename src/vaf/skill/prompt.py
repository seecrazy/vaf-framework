"""提示词托管（DESIGN §6.4）：版本化模板 + 通用规则注入 + 契约 Schema 注入 + 分级脱敏。"""
from __future__ import annotations

import json
import threading
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict

from ..security.redaction import Redactor, default_redactor
from .base import BaseSkill
from .context import SkillContext
from .memory import ErrorRule

CORE_TEMPLATE_ID = "vaf.system.core"


class PromptTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    version: int
    body: str
    active: bool = True


SYSTEM_TEMPLATE_BODY = """你是「{domain}」领域的专业人员，正在执行技能「{skill}」。严格遵循以下规则：
1. 输出必须严格符合 <output_schema> 定义的 JSON Schema，仅输出一个 JSON 对象，不要输出解释、Markdown 代码围栏或任何其他文本。
2. 非信息不足场景：必须给出至少 1 条 finding 与至少 1 条证伪条件（falsification）；findings 的 evidence_refs 必须引用 evidence 数组中存在的 ev_id。
3. 结论为 "{sentinel}"（信息不足）时：findings 与 falsification 可为空，但 clarifying_questions 必须至少 1 条，用于向信息提供方追问。
4. 所有数据缺失必须显式写入 data_gaps（item/reason/impact），不得沉默跳过或猜测。
5. 数据疑点（预处理已标记，务必纳入考量或澄清）：{anomalies}
6. 历史错误规避规则（必须遵守）：{error_rules}
7. 可用工具列表（未列出则不可调用）：{tools}
8. 用户补充的澄清回答（如有，优先采信）：{clarifications}

<output_schema>
{output_schema}
</output_schema>

领域专业指令：
{domain_prompt}"""


class PromptManager:
    def __init__(self, redactor: Optional[Redactor] = None):
        self._templates: Dict[str, Dict[int, PromptTemplate]] = {}
        self._lock = threading.RLock()  # active_version 会被持锁调用方复用
        self.redactor = redactor or default_redactor()
        self.register(
            PromptTemplate(template_id=CORE_TEMPLATE_ID, version=1, body=SYSTEM_TEMPLATE_BODY)
        )

    # ------------------------------------------------------------------
    def register(self, template: PromptTemplate) -> None:
        with self._lock:
            versions = self._templates.setdefault(template.template_id, {})
            if template.version in versions:
                raise ValueError(
                    f"duplicate template {template.template_id} v{template.version}"
                )
            # 新版本注册即激活（版本升级语义）
            for v in versions.values():
                v.active = False
            template.active = True
            versions[template.version] = template

    def activate_version(self, template_id: str, version: int) -> None:
        with self._lock:
            versions = self._templates.get(template_id)
            if not versions or version not in versions:
                raise KeyError(f"unknown template version {template_id} v{version}")
            for v in versions.values():
                v.active = v.version == version

    def active_version(self, template_id: str) -> int:
        with self._lock:
            versions = self._templates.get(template_id) or {}
            for v in versions.values():
                if v.active:
                    return v.version
            return max(versions.keys(), default=0)

    def versions_snapshot(self) -> Dict[str, int]:
        """执行计划快照用：各模板当前激活版本。"""
        with self._lock:
            return {tid: self.active_version(tid) for tid in self._templates}

    # ------------------------------------------------------------------
    def build(self, skill: BaseSkill, ctx: SkillContext) -> Tuple[str, int]:
        """组装系统提示词：模板（技能可路由）+ 通用规则 + Schema + 领域指令；按数据分级脱敏。

        技能声明的 prompt_template_id 未注册时回退核心模板（防御式，不崩溃）。
        """
        template_id = getattr(skill, "prompt_template_id", None) or CORE_TEMPLATE_ID
        version = self.active_version(template_id)
        with self._lock:
            template = self._templates.get(template_id, {}).get(version)
        if template is None:  # 未注册/未激活 → 回退核心模板
            template_id = CORE_TEMPLATE_ID
            version = self.active_version(template_id)
            with self._lock:
                template = dict(self._templates[template_id])[version]

        domain_prompt = skill.build_prompt(ctx)
        schema_text = json.dumps(
            skill.output_contract.model_json_schema(), ensure_ascii=False, indent=1
        )
        text = template.body.format(
            domain=ctx.domain,
            skill=skill.name,
            sentinel="insufficient_data",
            anomalies=_bullet_list(ctx.anomalies_text()) or "（无）",
            error_rules=_rule_lines(ctx.error_rules) or "（无）",
            tools=_bullet_list(ctx.available_tools) or "（无工具）",
            clarifications=json.dumps(ctx.clarifications, ensure_ascii=False)
            if ctx.clarifications
            else "（无）",
            output_schema=schema_text,
            domain_prompt=domain_prompt,
        )
        return self.redactor.apply(text, ctx.data_classification), version


def _bullet_list(items: List[str]) -> str:
    return "\n".join(f"- {i}" for i in items)


def _rule_lines(rules: List[ErrorRule]) -> str:
    return "\n".join(f"- [{r.kind}] {r.rule_text}" for r in rules)
