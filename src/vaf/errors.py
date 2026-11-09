"""VAF 异常体系：所有框架错误都收敛到该层级，便于领域与运行时统一处理。"""


class VafError(Exception):
    """框架基础异常。"""


class ContractError(VafError):
    """契约定义或校验错误（L1）。"""


class SchemaValidationError(VafError):
    """LLM 输出经自修复重试后仍不符合输出契约。"""

    def __init__(self, message: str, attempts: int = 0, last_errors: str = ""):
        super().__init__(message)
        self.attempts = attempts
        self.last_errors = last_errors


class ExpressionError(VafError):
    """条件表达式语法非法或运行期越权访问。"""


class PipelineValidationError(VafError):
    """Pipeline 配置校验失败（依赖环、引用缺失、高危默认值缺失等）。"""


class PlanMismatchError(VafError):
    """断点续跑时执行计划快照与当前环境不一致（版本漂移保护）。"""


class IdempotencyConflictError(VafError):
    """request_id 处于不可重放状态。"""


class PermissionDeniedError(VafError):
    """RBAC 权限不足或工具白名单校验失败。"""


class PolicyViolationError(VafError):
    """安全策略硬性拒绝（如数据分级与模型端点不兼容）。"""


class ToolError(VafError):
    """工具执行失败。"""


class ToolTimeoutError(ToolError):
    """工具执行超时。"""


class StreamViolationError(VafError):
    """流式生成滚动校验命中违规内容。"""


class BudgetExceededError(VafError):
    """Token 预算超限。"""


class MaxToolRoundsExceededError(VafError):
    """单技能工具调用轮次超上限。"""


class ReviewError(VafError):
    """人工审核任务状态非法或操作不允许。"""


class ClarificationError(VafError):
    """澄清任务状态非法或提交内容不合规。"""


class StepFailure(VafError):
    """编排步骤执行失败（含失败步骤名）。"""

    def __init__(self, step: str, message: str):
        super().__init__(f"step '{step}' failed: {message}")
        self.step = step
        self.message = message
