"""零代码领域接入（V2.2）：一个 YAML 文件定义完整领域，不写 Python。

声明面覆盖：输入/输出契约（字段表 → 动态 pydantic 模型，结论枚举自动追加哨兵）、
四类技能（提示词模板 + {{沙箱表达式}} 插值）、预处理（内置节点声明式拼装）、
人工审核/路由/交互/子流程/流式步骤（既有 YAML 步骤模型）、
证伪校验器（表达式）、结果合成（result_from）、终审（否决表达式 + 降级目标）。

逃生舱（仍需代码，按需）：自定义预处理节点、工具函数（register_tool）、
合成器多步映射。模板插值与条件表达式共用同一 AST 白名单沙箱。

用法：
    app.load_domain_file("domains/customer_ticket.domain.yaml")
    app.run("customer_ticket", {...}, request_id="...")
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Type, Union

from pydantic import BaseModel, ConfigDict, Field, create_model

from .contract.base import (
    INSUFFICIENT_DATA,
    Confidence,
    DomainInput,
    SkillOutput,
    conclusion_enum,
)
from .contract.falsification import FalsificationChecker
from .errors import ExpressionError, VafError
from .preprocessor import builtin as builtin_nodes
from .preprocessor.pipeline import Preprocessor
from .runtime.expression import SafeASTExpressionEngine
from .runtime.steps import Pipeline, pipeline_from_dict
from .skill.base import (
    AnalysisSkill,
    AuditResult,
    AuditSkill,
    BaseSkill,
    ExtractionSkill,
    GenerationSkill,
    TextOutput,
)
from .skill.context import SkillContext

__all__ = [
    "load_domain_dict",
    "load_domain_file",
    "render_template",
    "ExpressionChecker",
    "DomainSpec",
]


# ==========================================================================
# 1. 动态契约建模
# ==========================================================================
_SAFE_TYPES: Dict[str, type] = {
    "str": str, "string": str, "int": int, "integer": int,
    "float": float, "number": float, "bool": bool, "boolean": bool,
    "list": list, "array": list, "dict": dict, "object": dict,
}

_SENTINEL_NAME = "INSUFFICIENT_DATA"


def _make_enum(values: List[str], class_name: str, add_sentinel: bool) -> Type[Enum]:
    seen: List[str] = []
    for v in values:
        v = str(v)
        if v not in seen:
            seen.append(v)
    if add_sentinel and INSUFFICIENT_DATA not in seen:
        seen.append(INSUFFICIENT_DATA)
    members: Dict[str, str] = {}
    for i, v in enumerate(seen):
        name = v if v.isidentifier() else f"V{i}"
        if name in members:
            name = f"{name}_{i}"
        members[name] = v
    return Enum(class_name, members, type=str)  # type: ignore[arg-type]  # str 混入：枚举值可与字符串直接比较


def _field_from_spec(spec: "FieldSpec") -> tuple:
    """FieldSpec → (annotation, FieldInfo)。"""
    if spec.enum:
        annotation = _make_enum(spec.enum, "Enum_" + _ident(spec.name), add_sentinel=False)
    else:
        tname = (spec.type or "str").lower()
        if tname not in _SAFE_TYPES:
            raise VafError(
                f"字段 '{spec.name}' 类型 '{spec.type}' 不支持；"
                f"可用：{sorted(set(_SAFE_TYPES))} 或 enum 列表"
            )
        annotation = _SAFE_TYPES[tname]
    extra: Dict[str, Any] = {}
    if spec.min is not None:
        if annotation is str:
            extra["min_length"] = int(spec.min)
        else:
            extra["ge"] = spec.min
    if spec.max is not None:
        if annotation is str:
            extra["max_length"] = int(spec.max)
        else:
            extra["le"] = spec.max
    if spec.pattern:
        if annotation is not str:
            raise VafError(f"字段 '{spec.name}'：pattern 仅支持 str 类型")
        extra["pattern"] = spec.pattern
    field_kwargs: Dict[str, Any] = dict(extra)
    if spec.required and spec.default is None:
        field_kwargs["default"] = ...  # 必填且无默认
    elif spec.default is not None:
        default = spec.default
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            default = annotation(default)  # 枚举默认值转成员（序列化一致性）
        field_kwargs["default"] = default  # 显式 default 隐含可选
    elif annotation is list:
        field_kwargs["default_factory"] = list
    elif annotation is dict:
        field_kwargs["default_factory"] = dict
    elif annotation is str:
        field_kwargs["default"] = ""
    else:
        annotation = Optional[annotation]
        field_kwargs["default"] = None
    return (annotation, Field(description=spec.description or "", **field_kwargs))


def _ident(name: str) -> str:
    out = re.sub(r"\W+", "_", name)
    if out and out[0].isdigit():
        out = "F_" + out
    return out or "F"


def build_input_model(domain_name: str, spec: "InputSpec") -> Type[DomainInput]:
    fields = {f.name: _field_from_spec(f) for f in spec.fields}
    model = create_model(
        f"{_ident(domain_name)}_input",
        __base__=DomainInput,
        **fields,
    )
    return model


def build_output_model(domain_name: str, name: str, spec: "OutputSpec") -> Type[BaseModel]:
    cls_name = f"{_ident(domain_name)}_{_ident(name)}"
    extra_fields = {f.name: _field_from_spec(f) for f in spec.fields}
    if spec.kind == "entity":
        return create_model(cls_name, **extra_fields)
    if not spec.conclusion_enum:
        raise VafError(
            f"输出契约 '{name}'（analysis）必须声明 conclusion_enum（哨兵成员会自动追加）"
        )
    enum_cls = _make_enum(spec.conclusion_enum, cls_name + "Conclusion", add_sentinel=True)
    model = create_model(
        cls_name,
        __base__=SkillOutput,
        conclusion=(enum_cls, ...),
        **extra_fields,
    )
    # 哨兵成员自动追加 → 注册期校验必然通过
    return model


# ==========================================================================
# 2. 模板插值（{{表达式}}，与条件表达式共用沙箱）
# ==========================================================================
_PLACEHOLDER = re.compile(r"\{\{\s*(.+?)\s*\}\}")
_expr_engine = SafeASTExpressionEngine()


def _fmt(value: Any) -> str:
    import json

    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, BaseModel):
        return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, default=str)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if value is None:
        return "无"
    return str(value)


def render_template(text: str, namespace: Dict[str, Any]) -> str:
    def _sub(match: "re.Match[str]") -> str:
        expr_text = match.group(1)
        try:
            expr = _expr_engine.compile(expr_text)
            value = expr.evaluate(namespace)
        except ExpressionError as exc:
            raise VafError(
                f"提示词模板插值失败 '{{{{{expr_text}}}}}'：{exc}"
            ) from exc
        return _fmt(value)

    return _PLACEHOLDER.sub(_sub, text)


def _template_namespace(ctx: SkillContext, config_keys: List[str]) -> Dict[str, Any]:
    ns: Dict[str, Any] = {
        "input": ctx.domain_input,
        "upstream": dict(ctx.upstream_outputs),
        "clarifications": ctx.clarifications,
        "anomalies": ctx.anomalies_text(),
    }
    if config_keys:
        if ctx.config is None:
            raise VafError("技能声明了 config_keys 但运行环境未提供配置中心视图")
        ns["config"] = {k: ctx.config.get(k) for k in config_keys}
    return ns


# ==========================================================================
# 3. 配置化技能
# ==========================================================================
class ConfigAnalysisSkill(AnalysisSkill):
    def __init__(self, spec: "SkillSpec", output_contract: Type[BaseModel]):
        self.name = spec.name
        self.description = spec.description or ""
        self.version = spec.version
        self.output_contract = output_contract  # type: ignore[assignment]
        self._prompt = spec.prompt
        self._config_keys = list(spec.config_keys or [])
        self._tier = spec.model_tier
        self._tools = list(spec.tools or [])
        self._grounding = spec.grounding_required
        self.prompt_template_id = spec.prompt_template_id

    def tools(self) -> List[str]:
        return self._tools

    def model_tier(self) -> int:
        return self._tier

    def build_prompt(self, ctx: SkillContext) -> str:
        return render_template(self._prompt, _template_namespace(ctx, self._config_keys))


class ConfigExtractionSkill(ConfigAnalysisSkill, ExtractionSkill):
    pass


class ConfigGenerationSkill(GenerationSkill):
    def __init__(self, spec: "SkillSpec"):
        self.name = spec.name
        self.description = spec.description or ""
        self.version = spec.version
        self.output_contract = TextOutput  # type: ignore[assignment]
        self._prompt = spec.prompt
        self._config_keys = list(spec.config_keys or [])
        self._tier = spec.model_tier
        self._banned = [w.lower() for w in (spec.banned_words or [])]

    def model_tier(self) -> int:
        return self._tier

    def build_prompt(self, ctx: SkillContext) -> str:
        return render_template(self._prompt, _template_namespace(ctx, self._config_keys))

    def validate_chunk(self, buffer: str) -> List[str]:
        low = buffer.lower()
        return [f"命中违禁词：{w}" for w in self._banned if w in low]

    def validate_generation(self, content: str, ctx: SkillContext) -> List[str]:
        return self.validate_chunk(content)


class ConfigAuditSkill(AuditSkill):
    """配置化终审：veto_when 命中 → 否决并把结论降级为 downgrade_to。"""

    def __init__(self, spec: "SkillSpec", output_contract: Type[SkillOutput]):
        self.name = spec.name
        self.description = spec.description or ""
        self.version = spec.version
        self.output_contract = output_contract  # type: ignore[assignment]
        self._veto_when = spec.veto_when or "false"
        self._downgrade_to = spec.downgrade_to
        self._confidence = spec.confidence_after

    def build_prompt(self, ctx: SkillContext) -> str:  # pragma: no cover - 审核不走 LLM
        raise NotImplementedError

    def audit(self, target: SkillOutput, ctx: SkillContext) -> AuditResult:
        ns = {"output": target, "input": ctx.domain_input}
        try:
            expr = _expr_engine.compile(self._veto_when)
            veto = bool(expr.evaluate(ns))
        except ExpressionError as exc:
            raise VafError(f"审核技能 '{self.name}' veto_when 表达式错误：{exc}") from exc
        if not veto:
            return AuditResult(passed=True)
        if not self._downgrade_to:
            return AuditResult(
                passed=False, issues=[f"命中否决条件：{self._veto_when}"], revision=None
            )
        enum_cls = conclusion_enum(type(target))
        if enum_cls is None or self._downgrade_to not in {m.value for m in enum_cls}:
            raise VafError(
                f"审核技能 '{self.name}' downgrade_to='{self._downgrade_to}' "
                f"不在结论枚举 {sorted(m.value for m in enum_cls) if enum_cls else []} 中"
            )
        confidence = Confidence(self._confidence or "medium")
        revision = target.model_copy(
            update={
                "conclusion": enum_cls(self._downgrade_to),
                "confidence": confidence,
                "compliance_tags": target.compliance_tags + ["audit_downgraded"],
            }
        )
        return AuditResult(
            passed=False,
            issues=[f"命中否决条件：{self._veto_when}，降级为 {self._downgrade_to}"],
            revision=revision,
        )


# ==========================================================================
# 4. 预处理声明式拼装（内置节点工厂）
# ==========================================================================
def _node_range(spec: Dict[str, Any]):
    return builtin_nodes.RangeValidatorNode(
        field=spec["field"], low=spec.get("low"), high=spec.get("high"),
        level=spec.get("level", "warning"),
    )


def _node_presence(spec: Dict[str, Any]):
    from .contract.base import AnomalyLevel

    return builtin_nodes.FieldPresenceValidator(
        fields=list(spec.get("fields", [])), level=AnomalyLevel(spec.get("level", "warning"))
    )


def _node_keyword(spec: Dict[str, Any]):
    return builtin_nodes.KeywordFilterNode(spec["field"], spec.get("keywords", []))


def _node_dedup(spec: Dict[str, Any]):
    return builtin_nodes.HashDeduplicateNode(fields=spec.get("fields"))


def _node_pii(spec: Dict[str, Any]):
    return builtin_nodes.PIIDetectionNode(fields=spec.get("fields"))


def _node_truncate(spec: Dict[str, Any]):
    return builtin_nodes.LengthTruncateNode(spec["field"], max_chars=int(spec.get("max_chars", 2000)))


def _node_scorer(spec: Dict[str, Any]):
    return builtin_nodes.StaticScorerNode(
        spec["field"], dict(spec.get("map", {})), default_score=float(spec.get("default", 0))
    )


NODE_FACTORIES: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "range": _node_range,
    "field_presence": _node_presence,
    "keyword_filter": _node_keyword,
    "hash_dedup": _node_dedup,
    "pii_detection": _node_pii,
    "length_truncate": _node_truncate,
    "static_scorer": _node_scorer,
}


def assemble_preprocessor(spec: Optional["PreprocessorSpec"]) -> Optional[Callable[[], Preprocessor]]:
    if spec is None:
        return None

    def _build(spec=spec) -> Preprocessor:
        pre = Preprocessor()
        for entry in spec.filters or []:
            pre.filters.append(_make_node(entry, "filters"))
        for entry in spec.deduplicators or []:
            pre.deduplicators.append(_make_node(entry, "deduplicators"))
        for entry in spec.enrichers or []:
            pre.enrichers.append(_make_node(entry, "enrichers"))
        for entry in spec.validators or []:
            pre.validators.append(_make_node(entry, "validators"))
        if spec.scorer:
            pre.scorer = _make_node(spec.scorer, "scorer")
        return pre

    return _build


def _make_node(entry: Dict[str, Any], section: str) -> Any:
    node_type = entry.get("type")
    factory = NODE_FACTORIES.get(node_type or "")
    if factory is None:
        raise VafError(
            f"预处理 {section} 节点 type='{node_type}' 未注册；"
            f"可用：{sorted(NODE_FACTORIES)}（自定义节点请写 Python）"
        )
    params = {k: v for k, v in entry.items() if k != "type"}
    try:
        return factory(params)
    except (KeyError, TypeError, ValueError) as exc:
        raise VafError(f"预处理 {section} 节点 type='{node_type}' 参数错误：{exc}") from exc


# ==========================================================================
# 5. 表达式证伪校验器
# ==========================================================================
class ExpressionChecker(FalsificationChecker):
    """when 表达式命中（返回真）即触发证伪；命名空间 {input, vars}。"""

    checker_id = "__expression__"  # 类级占位（子类校验用）；实例构造时覆盖

    def __init__(self, checker_id: str, when: str):
        self.checker_id = checker_id
        self._when = when

    def check(self, context: Dict[str, Any]) -> bool:
        ns = {
            "input": context.get("input", {}) or {},
            "vars": context.get("vars", {}) or {},
        }
        try:
            return bool(_expr_engine.evaluate(self._when, ns))
        except ExpressionError:
            return False  # 上下文缺字段时不误触发（配置错误在注册期已预编译校验）


# ==========================================================================
# 6. 领域规格模型（结构校验）与装配
# ==========================================================================
class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = "str"
    required: bool = True
    default: Optional[Any] = None
    description: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    pattern: Optional[str] = None
    enum: Optional[List[str]] = None


class InputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: List[FieldSpec] = Field(default_factory=list)


class OutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "analysis"  # analysis（SkillOutput）| entity（抽取实体）
    description: str = ""
    conclusion_enum: List[str] = Field(default_factory=list)
    fields: List[FieldSpec] = Field(default_factory=list)


class SkillSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = "analysis"  # analysis | extraction | generation | audit
    description: str = ""
    version: str = "1.0.0"
    output: Optional[str] = None  # generation 默认 TextOutput
    prompt: str = ""
    model_tier: int = 1
    tools: List[str] = Field(default_factory=list)
    grounding_required: bool = False
    config_keys: List[str] = Field(default_factory=list)
    prompt_template_id: Optional[str] = None
    banned_words: List[str] = Field(default_factory=list)
    veto_when: Optional[str] = None          # audit 专用
    downgrade_to: Optional[str] = None       # audit 专用
    confidence_after: Optional[str] = None   # audit 专用：medium/low


class PreprocessorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: List[Dict[str, Any]] = Field(default_factory=list)
    deduplicators: List[Dict[str, Any]] = Field(default_factory=list)
    enrichers: List[Dict[str, Any]] = Field(default_factory=list)
    validators: List[Dict[str, Any]] = Field(default_factory=list)
    scorer: Optional[Dict[str, Any]] = None


class CheckerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checker_id: str
    when: str


class DomainSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    version: str = "1.0.0"
    data_classification: str = "internal"
    input: InputSpec = Field(default_factory=InputSpec)
    outputs: Dict[str, OutputSpec] = Field(default_factory=dict)
    preprocessor: Optional[PreprocessorSpec] = None
    skills: List[SkillSpec] = Field(default_factory=list)
    falsification_checkers: List[CheckerSpec] = Field(default_factory=list)
    pipeline: Dict[str, Any]
    result_from: Optional[str] = None


def load_domain_dict(
    data: Union[Dict[str, Any], DomainSpec],
    *,
    register_checkers_to: Optional[Any] = None,
):
    """装配一个零代码领域，返回 register_domain 所需的全部构件。"""
    try:
        spec = data if isinstance(data, DomainSpec) else DomainSpec.model_validate(data)
    except Exception as exc:
        raise VafError(f"领域配置结构非法：{exc}") from exc

    # 预编译校验：checker 表达式、audit veto 表达式、技能模板插值表达式
    for checker in spec.falsification_checkers:
        try:
            _expr_engine.compile(checker.when)
        except ExpressionError as exc:
            raise VafError(f"证伪校验器 '{checker.checker_id}' when 表达式非法：{exc}") from exc
    for skill in spec.skills:
        if skill.type == "audit" and skill.veto_when:
            try:
                _expr_engine.compile(skill.veto_when)
            except ExpressionError as exc:
                raise VafError(f"审核技能 '{skill.name}' veto_when 非法：{exc}") from exc
        for m in _PLACEHOLDER.finditer(skill.prompt or ""):
            try:
                _expr_engine.compile(m.group(1))
            except ExpressionError as exc:
                raise VafError(
                    f"技能 '{skill.name}' 提示词插值 '{{{{{m.group(1)}}}}}' 非法：{exc}"
                ) from exc

    input_model = build_input_model(spec.domain, spec.input)
    outputs: Dict[str, Type[BaseModel]] = {}
    for name, out_spec in spec.outputs.items():
        outputs[name] = build_output_model(spec.domain, name, out_spec)

    skills: List[BaseSkill] = []
    for s in spec.skills:
        if s.type == "analysis":
            contract = _require_output(outputs, s, analysis=True)
            skills.append(ConfigAnalysisSkill(s, contract))
        elif s.type == "extraction":
            contract = _require_output(outputs, s, analysis=False)
            skills.append(ConfigExtractionSkill(s, contract))
        elif s.type == "generation":
            skills.append(ConfigGenerationSkill(s))
        elif s.type == "audit":
            contract = _require_output(outputs, s, analysis=True)
            skills.append(ConfigAuditSkill(s, contract))  # type: ignore[arg-type]
        else:
            raise VafError(f"技能 '{s.name}' type '{s.type}' 不支持（analysis/extraction/generation/audit）")
    names = [s.name for s in skills]
    if len(set(names)) != len(names):
        raise VafError(f"技能名重复：{names}")

    pipeline = pipeline_from_dict(dict(spec.pipeline))
    audit_names = [s.name for s in spec.skills if s.type == "audit"]
    if audit_names and not pipeline.audit_skill:
        pipeline = pipeline.model_copy(update={"audit_skill": audit_names[0]})

    # result_from 合成器
    result_from = spec.result_from
    if not result_from:
        terminal_steps = [st for st in pipeline.steps if not any(
            st.name in (other.depends_on or []) for other in pipeline.steps
        )]
        result_from = terminal_steps[-1].name if terminal_steps else None

    def _synthesizer(state, typed_outputs, *, _step=result_from):
        output = typed_outputs.get(_step)
        if isinstance(output, BaseModel):
            return output
        raise VafError(
            f"result_from 步骤 '{_step}' 无可用输出（状态请用 vaf inspect 排查）"
        )

    checkers = [
        ExpressionChecker(c.checker_id, c.when) for c in spec.falsification_checkers
    ]
    if register_checkers_to is not None:
        for checker in checkers:
            register_checkers_to.register(checker)

    return {
        "name": spec.domain,
        "input_contract": input_model,
        "pipeline": pipeline,
        "skills": skills,
        "preprocessor": assemble_preprocessor(spec.preprocessor),
        "synthesizer": _synthesizer,
        "audit_skill": pipeline.audit_skill,
        "data_classification": spec.data_classification,
        "checkers": checkers,
        "spec": spec,
    }


def load_domain_file(path: str, *, register_checkers_to: Optional[Any] = None):
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise VafError(f"领域文件必须是 YAML 映射：{path}")
    return load_domain_dict(data, register_checkers_to=register_checkers_to)


def _require_output(
    outputs: Dict[str, Type[BaseModel]], skill: SkillSpec, *, analysis: bool
) -> Type[BaseModel]:
    if not skill.output or skill.output not in outputs:
        raise VafError(
            f"技能 '{skill.name}' 引用了未声明的输出契约 '{skill.output}'"
            f"（contracts.outputs 中可用：{sorted(outputs)}）"
        )
    contract = outputs[skill.output]
    if analysis and not (isinstance(contract, type) and issubclass(contract, SkillOutput)):
        raise VafError(
            f"技能 '{skill.name}' 需要 analysis 输出契约（kind: analysis），"
            f"'{skill.output}' 是 entity"
        )
    return contract
