"""安全表达式引擎测试（B5）：合法语法 / 缺失路径 / 攻击面。"""
import pytest

from vaf.errors import ExpressionError
from vaf.runtime.expression import SafeASTExpressionEngine


@pytest.fixture()
def engine():
    return SafeASTExpressionEngine()


NS = {
    "steps": {
        "dx": {"output": {"conclusion": "emergency", "confidence": "high", "data_gaps": [1, 2]},
               "status": "succeeded"},
        "none_out": {"output": None, "status": "degraded"},
    },
    "vars": {"public": {"tenant": "acme"}, "secret": "x"},
    "routers": {"r1": {"selected": "deep_probe"}},
    "input": {"age": 58},
    "loop_count": 1,
}


def test_field_compare_and_logic(engine):
    assert engine.evaluate("steps.dx.output.conclusion == 'emergency'", NS)
    assert engine.evaluate("steps.dx.output.confidence == 'high' and input.age > 18", NS)
    assert engine.evaluate("not (input.age < 18)", NS)
    assert engine.evaluate("input.age >= 58 or false", NS)


def test_in_operator_and_len(engine):
    assert engine.evaluate("steps.dx.output.conclusion in ['emergency', 'urgent']", NS)
    assert engine.evaluate("len(steps.dx.output.data_gaps) > 1", NS)
    assert engine.evaluate("'deep' in routers.r1.selected", NS)


def test_subscript_access(engine):
    assert engine.evaluate("vars['public']['tenant'] == 'acme'", NS)
    assert engine.evaluate("steps.dx.output['confidence'] == 'high'", NS)


def test_arithmetic_and_functions(engine):
    assert engine.evaluate("len(steps.dx.output.data_gaps) + input.age == 60", NS)
    assert engine.evaluate("min(1, 2) == 1", NS)
    assert engine.evaluate("str(input.age) == '58'", NS)


def test_loop_count_namespace(engine):
    assert engine.evaluate("loop_count < 2", NS)


def test_missing_path_raises(engine):
    with pytest.raises(ExpressionError, match="missing"):
        engine.evaluate("steps.none_out.output.conclusion == 'x'", NS)


def test_unknown_root_name(engine):
    with pytest.raises(ExpressionError, match="unknown name"):
        engine.evaluate("__import__ == 1", NS)


# ---------------------------------------------------------------- 攻击面
def test_private_attribute_forbidden(engine):
    with pytest.raises(ExpressionError, match="private attribute"):
        engine.evaluate("steps.__class__", NS)
    with pytest.raises(ExpressionError):
        engine.compile("input.__dict__")


def test_arbitrary_call_forbidden(engine):
    with pytest.raises(ExpressionError, match="whitelisted"):
        engine.compile("open('/etc/passwd')")
    with pytest.raises(ExpressionError):
        engine.compile("eval('1+1')")


def test_lambda_and_import_forbidden(engine):
    with pytest.raises(ExpressionError):
        engine.compile("(lambda: 1)()")
    with pytest.raises(ExpressionError):
        engine.compile("__import__('os').system('ls')")


def test_comprehension_forbidden(engine):
    with pytest.raises(ExpressionError):
        engine.compile("[x for x in input]")


def test_syntax_error_reported(engine):
    with pytest.raises(ExpressionError, match="invalid expression syntax"):
        engine.compile("steps.dx.output.conclusion === 'x'")


def test_compile_time_validation_catches_disallowed(engine):
    # 编译期（而非求值期）拒绝
    with pytest.raises(ExpressionError):
        engine.compile("vars.secret.startswith('x')")
