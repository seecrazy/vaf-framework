"""LLM 层测试：Mock 脚本化、模型路由 fail-closed（B3）、熔断降级。"""
import pytest

from vaf.errors import PolicyViolationError
from vaf.llm.mock import ScriptedMockLLM, tool_call
from vaf.llm.router import CircuitBreakerConfig, ModelRouter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def test_mock_scripted_responses_and_stream():
    mock = ScriptedMockLLM(script=["第一段", "第二段"], chunk_size=3)
    resp = mock.call([{"role": "user", "content": "hi"}])
    assert resp.content == "第一段"
    chunks = list(mock.stream([{"role": "user", "content": "hi"}]))
    assert "".join(chunks) == "第二段"


def test_mock_tool_calls_and_exception_injection():
    mock = ScriptedMockLLM(script=[[tool_call("t", {"a": 1})], RuntimeError("boom")])
    resp = mock.call([])
    assert resp.tool_calls[0].name == "t"
    with pytest.raises(RuntimeError):
        mock.call([])


# ---------------------------------------------------------------- 路由
def test_route_fail_closed_on_classification():
    router = ModelRouter()
    router.register("public-only", ScriptedMockLLM("p"), tier=1,
                    allowed_classifications={"public", "internal"})
    with pytest.raises(PolicyViolationError, match="no model endpoint"):
        router.route(preferred_tier=1, data_classification="confidential")


def test_route_tier_preference():
    router = ModelRouter()
    router.register("light", ScriptedMockLLM("l"), tier=1)
    router.register("heavy", ScriptedMockLLM("h"), tier=2)
    assert router.route(preferred_tier=2).name == "heavy"
    assert router.route(preferred_tier=1).name == "light"


def test_breaker_opens_on_error_rate_and_recovers():
    clock = FakeClock()
    router = ModelRouter(CircuitBreakerConfig(min_samples=3, error_rate_threshold=0.5, open_seconds=60), clock=clock)
    router.register("flaky", ScriptedMockLLM("f"), tier=1)
    for _ in range(3):
        router.record_failure("flaky")
    assert router.breaker_state("flaky") == "open"
    clock.advance(61)  # 冷却期结束 → half-open 允许探测
    assert router.breaker_state("flaky") == "half_open"


def test_open_breaker_falls_back_to_other_endpoint():
    clock = FakeClock()
    router = ModelRouter(CircuitBreakerConfig(min_samples=2, error_rate_threshold=0.5), clock=clock)
    router.register("a", ScriptedMockLLM("a"), tier=1)
    router.register("b", ScriptedMockLLM("b"), tier=2)
    router.record_failure("a")
    router.record_failure("a")
    assert router.breaker_state("a") == "open"
    endpoint = router.route(preferred_tier=1)
    assert endpoint.name == "b"


def test_route_digest_stable():
    router = ModelRouter()
    router.register("x", ScriptedMockLLM("x"), tier=1)
    d1 = router.route_digest()
    router.register("y", ScriptedMockLLM("y"), tier=1)
    assert router.route_digest() != d1
