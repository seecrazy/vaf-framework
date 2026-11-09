"""技能执行器（DESIGN §6.2 / §9）：提示词组装 → 工具循环 → 结构化解析 → 自修复重试。

自修复重试（B1）：JSON 解析 / Schema 校验失败时，把模型原始输出与校验错误明细
回喂给模型，最多 max_repair_attempts 轮；仍失败抛 SchemaValidationError，由
引擎按步骤 on_schema_fail 策略处理。

流式（A4）：checkpoint 滚动校验 + holdback 尾部缓冲，违规即熔断流。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError

from ..contract.base import FalsificationCondition, SkillOutput
from ..contract.falsification import FalsificationEngine
from ..errors import (
    BudgetExceededError,
    MaxToolRoundsExceededError,
    SchemaValidationError,
    StreamViolationError,
)
from ..llm.client import LLMClient, LLMResponse, Message
from ..llm.router import ModelRouter
from ..observe.metrics import MetricsRegistry, default_metrics
from ..observe.trace import Span, TraceCollector
from ..tools.adapter import ToolAdapter, ToolContext
from .base import BaseSkill, GenerationSkill, TextOutput
from .context import SkillContext
from .grounding import GroundingChecker
from .memory import ErrorMemory
from .prompt import PromptManager

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def strip_fences(text: str) -> str:
    return _FENCE.sub("", text.strip()).strip()


class SkillExecutor:
    def __init__(
        self,
        *,
        router: ModelRouter,
        prompt_manager: PromptManager,
        tool_adapter: ToolAdapter,
        falsification: Optional[FalsificationEngine] = None,
        error_memory: Optional[ErrorMemory] = None,
        grounding: Optional[GroundingChecker] = None,
        metrics: Optional[MetricsRegistry] = None,
        traces: Optional[TraceCollector] = None,
        max_tool_rounds: int = 5,
        max_repair_attempts: int = 2,
        token_budget: Optional[int] = None,
    ):
        self.router = router
        self.prompts = prompt_manager
        self.tools = tool_adapter
        self.falsification = falsification
        self.error_memory = error_memory or ErrorMemory()
        self.grounding = grounding or GroundingChecker()
        self._metrics = metrics or default_metrics()
        self._traces = traces
        self.max_tool_rounds = max_tool_rounds
        self.max_repair_attempts = max_repair_attempts
        self.token_budget = token_budget
        self._tokens_used = 0
        self._repair_ok = self._metrics.counter("llm_repair_success", "自修复成功次数")
        self._repair_fail = self._metrics.counter("llm_repair_exhausted", "自修复耗尽次数")
        self._llm_calls = self._metrics.counter("llm_calls_total", "LLM 调用次数")
        self._tokens = self._metrics.counter("llm_tokens_total", "LLM token 用量")

    # ------------------------------------------------------------------
    def execute(self, skill: BaseSkill, ctx: SkillContext, parent_span: Optional[Span] = None) -> BaseModel:
        span = self._span(f"skill:{skill.name}", parent_span, skill=skill.name)
        try:
            output = self._execute_inner(skill, ctx, span)
            span.finish("ok")
            return output
        except Exception as exc:
            span.finish("error", error=str(exc))
            raise

    def _execute_inner(self, skill: BaseSkill, ctx: SkillContext, span: Span) -> BaseModel:
        system_prompt, _prompt_version = self.prompts.build(skill, ctx)
        messages: List[Message] = [{"role": "system", "content": system_prompt}]
        tool_defs = (
            self.tools.registry.definitions(skill.tools())
            if skill.tools() and self._client_for(skill, ctx).has("tools")
            else None
        )

        for round_no in range(self.max_tool_rounds):
            response = self._call_llm(skill, ctx, messages, tool_defs=tool_defs)

            if response.tool_calls:
                tool_ctx = ToolContext(
                    request_id=ctx.request_id,
                    skill_name=skill.name,
                    allowed_tools=skill.tools(),
                    user=None,
                    data_classification=ctx.data_classification,
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": [
                            {
                                "id": tc.call_id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                                },
                            }
                            for tc in response.tool_calls
                        ],
                    }
                )
                for tc in response.tool_calls:
                    result = self.tools.call(tc, tool_ctx)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    )
                continue

            # 结构化解析 + 自修复重试
            output = self._parse_with_repair(skill, ctx, messages, response)
            self._post_validate(skill, ctx, output)
            return output

        raise MaxToolRoundsExceededError(
            f"skill '{skill.name}' exceeded max tool rounds ({self.max_tool_rounds})"
        )

    # ------------------------------------------------------------------
    def _client_for(self, skill: BaseSkill, ctx: SkillContext) -> LLMClient:
        endpoint = self.router.route(
            preferred_tier=skill.model_tier(),
            data_classification=ctx.data_classification,
        )
        return endpoint.client

    def _call_llm(
        self,
        skill: BaseSkill,
        ctx: SkillContext,
        messages: List[Message],
        *,
        tool_defs: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        endpoint = self.router.route(
            preferred_tier=skill.model_tier(),
            data_classification=ctx.data_classification,
        )
        client = endpoint.client
        schema = None
        if client.has("json"):
            schema = skill.output_contract.model_json_schema()
        try:
            response = client.call(messages, tools=tool_defs, response_schema=schema)
        except Exception:
            self.router.record_failure(endpoint.name)
            raise
        self.router.record_success(endpoint.name)
        self._llm_calls.inc(skill=skill.name, endpoint=endpoint.name)
        if response.usage:
            self._tokens.inc(skill=skill.name, kind="prompt", value=response.usage.prompt_tokens)
            self._tokens.inc(skill=skill.name, kind="completion", value=response.usage.completion_tokens)
            self._tokens_used += response.usage.total_tokens
            if self.token_budget is not None and self._tokens_used > self.token_budget:
                raise BudgetExceededError(
                    f"token budget exceeded: {self._tokens_used} > {self.token_budget}"
                )
        return response

    # ------------------------------------------------------------------
    def _parse_with_repair(
        self,
        skill: BaseSkill,
        ctx: SkillContext,
        messages: List[Message],
        response: LLMResponse,
    ) -> BaseModel:
        raw = response.content or ""
        contract: Type[BaseModel] = skill.output_contract
        last_errors = ""
        for attempt in range(self.max_repair_attempts + 1):
            try:
                cleaned = strip_fences(raw)
                parsed = json.loads(cleaned) if cleaned.strip() else {}
                output = contract.model_validate(parsed)
                if attempt > 0:
                    self._repair_ok.inc(skill=skill.name)
                return output
            except (json.JSONDecodeError, ValidationError) as exc:
                last_errors = _format_validation_error(exc)
                if attempt < self.max_repair_attempts:
                    messages.append({"role": "assistant", "content": raw})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "你的输出违反了输出契约：\n"
                                f"{last_errors}\n"
                                "请仅输出修正后的完整 JSON 对象，不要任何解释或 Markdown 围栏。"
                            ),
                        }
                    )
                    response = self._call_llm(skill, ctx, messages)
                    raw = response.content or ""
        self._repair_fail.inc(skill=skill.name)
        self.error_memory.add_schema_error(
            scope=skill.name,
            pattern=contract.__name__,
            rule_text=f"输出必须符合 {contract.__name__} 契约（最近错误：{last_errors[:120]}）",
        )
        raise SchemaValidationError(
            f"skill '{skill.name}' output failed schema validation after "
            f"{self.max_repair_attempts} repair attempts",
            attempts=self.max_repair_attempts,
            last_errors=last_errors,
        )

    # ------------------------------------------------------------------
    def _post_validate(self, skill: BaseSkill, ctx: SkillContext, output: BaseModel) -> None:
        """证伪 checker 引用 fail-fast + grounding 校验。"""
        if isinstance(output, SkillOutput) and self.falsification is not None:
            self.falsification.validate_references(output.falsification)
        if skill.grounding_required and isinstance(output, SkillOutput):
            pool = [json.dumps(p.item.payload, ensure_ascii=False, default=str) for p in ctx.preprocessed]
            pool += [
                json.dumps(m.model_dump(mode="json"), ensure_ascii=False, default=str)
                for m in ctx.upstream_outputs.values()
            ]
            report = self.grounding.verify(output, pool)
            if report.score < skill.grounding_min:
                raise SchemaValidationError(
                    f"skill '{skill.name}' grounding score {report.score} < min {skill.grounding_min}: "
                    f"unsupported findings={report.unsupported_findings}, "
                    f"unresolved evidence={report.unresolved_evidence}"
                )

    # ------------------------------------------------------------------
    # 流式生成（A4）：checkpoint 滚动校验 + holdback 尾部缓冲
    # ------------------------------------------------------------------
    def stream_execute(
        self,
        skill: GenerationSkill,
        ctx: SkillContext,
        on_token: Callable[[str], None],
        *,
        checkpoint_size: int = 80,
        holdback_size: int = 24,
        on_violation: str = "abort",
        parent_span: Optional[Span] = None,
    ) -> TextOutput:
        span = self._span(f"skill:{skill.name}:stream", parent_span, skill=skill.name)
        client = self._client_for(skill, ctx)
        if not client.has("stream"):
            output = self.execute(skill, ctx, parent_span)
            if isinstance(output, TextOutput):
                on_token(output.text)
                return output
            raise TypeError("stream_execute requires stream-capable client or TextOutput contract")

        system_prompt, _ = self.prompts.build(skill, ctx)
        messages: List[Message] = [{"role": "system", "content": system_prompt}]

        buffer = ""
        released = 0  # 已对消费者释放的字符数
        violations: List[str] = []
        degraded = False

        def _flush(valid_boundary: int) -> None:
            nonlocal released
            if valid_boundary > released:
                on_token(buffer[released:valid_boundary])
                released = valid_boundary

        try:
            for token in client.stream(messages):
                buffer += token
                while len(buffer) - released >= checkpoint_size + holdback_size:
                    checkpoint = len(buffer) - holdback_size
                    issues = skill.validate_chunk(buffer[:checkpoint])
                    if issues:
                        if on_violation == "abort":
                            raise StreamViolationError(
                                f"streaming content violated policy: {issues[:3]}"
                            )
                        violations.extend(issues)
                        degraded = True
                    _flush(checkpoint)
            # 流结束：终检全文
            issues = skill.validate_chunk(buffer)
            if issues and on_violation == "abort":
                raise StreamViolationError(f"streaming content violated policy: {issues[:3]}")
            violations.extend(issues)
            final_issues = skill.validate_generation(buffer, ctx)
            violations.extend(final_issues)
            degraded = degraded or bool(final_issues)
            _flush(len(buffer))
            span.finish("ok" if not degraded else "degraded", violations=violations[:3])
            return TextOutput(text=buffer, compliance_flags=violations, degraded=degraded)
        except StreamViolationError:
            span.finish("error")
            raise

    # ------------------------------------------------------------------
    def _span(self, name: str, parent: Optional[Span], **attrs: Any) -> Span:
        if self._traces is not None:
            return self._traces.start(name, parent, **attrs)
        return Span(name=name, attributes=attrs)


def _format_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        lines = []
        for err in exc.errors()[:10]:
            loc = ".".join(str(x) for x in err["loc"])
            lines.append(f"- {loc}: {err['msg']}")
        return "\n".join(lines)
    return f"JSON 解析失败: {exc}"
