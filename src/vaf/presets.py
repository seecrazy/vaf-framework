"""预置领域库（V2.3）：开箱即用的领域默认配置 + 离线体验。

- 预置内容基于真实领域知识撰写并在 index.yaml 声明知识依据（knowledge），
  每个领域文件头部注明校准要求——预置是起点，不是免审通行证；
- 零代码 YAML 为推荐路径；credit_review 另附代码风格孪生实现
  （presets/credit_review/python/）供"同一领域、两种接入方式"对照学习；
- `try`：离线即体验（ScriptedMockLLM + 样例输入/响应，零外部依赖）；
- `install`：复制 YAML 到 domains/ 目录即被自动发现，接上真实 LLM 即 0 分钟使用。

查找顺序：环境变量 VAF_PRESETS_DIR → 仓库根 presets/（源码运行）。
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .errors import VafError

__all__ = [
    "presets_dir",
    "preset_samples",
    "preset_eval",
    "load_index",
    "preset_path",
    "install_preset",
    "try_preset",
    "PresetResponder",
]

_SKILL_MARK = re.compile(r"正在执行技能「([^」]+)」")


def presets_dir(explicit: Optional[str] = None) -> Path:
    """预置库目录：--presets-dir / VAF_PRESETS_DIR / 仓库根 presets/。"""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("VAF_PRESETS_DIR")
    if env:
        candidates.append(Path(env))
    # src/vaf/presets.py → 仓库根
    candidates.append(Path(__file__).resolve().parent.parent.parent / "presets")
    for path in candidates:
        if path.is_dir() and (path / "index.yaml").exists():
            return path
    raise VafError(
        f"未找到预置库目录（尝试过：{[str(c) for c in candidates]}）；"
        f"可用 VAF_PRESETS_DIR 指定"
    )


def load_index(explicit_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    with open(presets_dir(explicit_dir) / "index.yaml", "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    presets = data.get("presets") or []
    for entry in presets:
        if not preset_path(entry["name"], explicit_dir).exists() and not (
            package_dir(entry["name"], explicit_dir) / "__init__.py"
        ).exists():
            raise VafError(f"预置领域 '{entry['name']}' 的 YAML/Python 包均缺失")
    return presets


def package_dir(name: str, explicit_dir: Optional[str] = None) -> Path:
    """Python 变体：预置目录下的同名包目录（含 __init__.py 与 register）。"""
    return presets_dir(explicit_dir) / name / name




def preset_samples(name: str, explicit_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取预置领域的全部样例输入（含样例名），供 CLI 展示与测试复用。"""
    samples_file = samples_path(name, explicit_dir)
    if not samples_file.exists():
        raise VafError(f"预置领域 '{name}' 缺少 samples.yaml")
    with open(samples_file, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    inputs = data.get("inputs") or []
    if not inputs:
        raise VafError(f"预置领域 '{name}' 样例为空")
    return inputs


def _write_sample_input(name: str, target: Path, explicit_dir: Optional[str]) -> None:
    """随安装附带一份样例输入（取 samples.yaml 第一个样例），vaf run 可直接使用。"""
    import json

    samples_file = samples_path(name, explicit_dir)
    if not samples_file.exists():
        return
    with open(samples_file, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    inputs = data.get("inputs") or []
    if not inputs:
        return
    path = target / f"{name}.sample-input.json"
    path.write_text(
        json.dumps(inputs[0]["input"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def preset_kind(name: str, explicit_dir: Optional[str] = None) -> str:
    """lowcode（*.domain.yaml）| python（同名包目录）。"""
    if preset_path(name, explicit_dir).exists():
        return "lowcode"
    if (package_dir(name, explicit_dir) / "__init__.py").exists():
        return "python"
    raise VafError(f"预置领域 '{name}' 既无 YAML 也无 Python 包")


def preset_path(name: str, explicit_dir: Optional[str] = None) -> Path:
    return presets_dir(explicit_dir) / name / f"{name}.domain.yaml"


def samples_path(name: str, explicit_dir: Optional[str] = None) -> Path:
    return presets_dir(explicit_dir) / name / "samples.yaml"


def install_preset(name: str, target_dir: str = "domains", explicit_dir: Optional[str] = None) -> Path:
    """安装预置到目标目录（默认 domains/，即被自动发现）：YAML 复制文件 / Python 复制包目录。"""
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    _write_sample_input(name, target, explicit_dir)
    if preset_kind(name, explicit_dir) == "lowcode":
        src = preset_path(name, explicit_dir)
        dest = target / src.name
        if dest.exists():
            raise VafError(f"目标已存在：{dest}（如需覆盖请手动删除）")
        shutil.copyfile(src, dest)
        return dest
    src_pkg = package_dir(name, explicit_dir)
    dest = target / src_pkg.name
    if dest.exists():
        raise VafError(f"目标已存在：{dest}（如需覆盖请手动删除）")
    shutil.copytree(src_pkg, dest)
    return dest


# ---------------------------------------------------------------------------
# 离线体验：样例驱动
# ---------------------------------------------------------------------------
class PresetResponder:
    """按技能名路由的脚本化响应器；responses[skill] 按调用序弹出，末项复用。"""

    def __init__(self, responses: Dict[str, List[str]]):
        self._responses = {k: list(v) for k, v in responses.items()}
        self._cursor: Dict[str, int] = {}

    def __call__(self, messages, tools, response_schema, **opts):
        system = messages[0]["content"] if messages else ""
        match = _SKILL_MARK.search(system)
        skill = match.group(1) if match else "<unknown>"
        queue = self._responses.get(skill)
        if not queue:
            raise VafError(
                f"样例响应缺少技能 '{skill}' 的脚本（检查 presets/<领域>/samples.yaml）"
            )
        idx = self._cursor.get(skill, 0)
        self._cursor[skill] = idx + 1
        return queue[idx] if idx < len(queue) else queue[-1]


def try_preset(
    name: str,
    *,
    presets_root: Optional[str] = None,
    app=None,
):
    """离线运行预置领域的全部样例，返回 [(样例名, RunHandle)]。

    不写入 domains/、不触碰状态库（内存态），零副作用。
    """
    from .app import VafApp
    from .llm.mock import ScriptedMockLLM

    samples_file = samples_path(name, presets_root)
    if not samples_file.exists():
        raise VafError(f"预置领域 '{name}' 缺少样例文件 samples.yaml，无法离线体验")
    with open(samples_file, "r", encoding="utf-8") as fh:
        samples = yaml.safe_load(fh) or {}
    inputs = samples.get("inputs") or []
    if not inputs:
        raise VafError(f"预置领域 '{name}' 样例为空")

    if app is None:
        app = VafApp()
        app.register_model_endpoint(
            "preset-mock",
            ScriptedMockLLM(name="preset-mock", responder=PresetResponder(samples.get("responses") or {})),
            tier=1,
        )
    _load_preset_domain(app, name, presets_root)

    results = []
    for sample in inputs:
        handle = app.run(name, sample["input"], f"preset-try:{name}:{sample['input'].get('raw_id', '')}")
        results.append((sample.get("name", sample["input"].get("raw_id", "?")), handle))
    return results


def _load_preset_domain(app, name: str, presets_root: Optional[str]) -> None:
    """按变体加载：lowcode → load_domain_file；python → 导入同名包并 register。"""
    import importlib
    import sys

    if preset_kind(name, presets_root) == "lowcode":
        app.load_domain_file(str(preset_path(name, presets_root)))
        return
    pkg_root = str(presets_dir(presets_root) / name)
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)
    module = importlib.import_module(name)
    if not hasattr(module, "register"):
        raise VafError(f"Python 预置 '{name}' 缺少 register(app) 入口")
    module.register(app)


def preset_eval(
    name: str,
    *,
    presets_root: Optional[str] = None,
    include_golden: bool = True,
):
    """黄金集评估。

    返回 (EvalReport, mode)：mode ∈ {"offline", "live"}。
    - live：环境配了 VAF_LLM_*（或调用方已注册真实端点）→ 跑全部用例
      （samples.expected + golden.yaml 扩充用例），输出真实准确率；
    - offline：仅跑 samples（脚本化响应），accuracy 恒 1.0，仅验证机制与门禁；
      真实基线必须配 VAF_LLM_BASE_URL/API_KEY/MODEL 后运行。
    """
    import os

    from .app import VafApp
    from .eval.dataset import GoldenCase
    from .eval.dataset import GoldenDataset  # noqa: F401
    from .eval.runner import EvalRunner, Gate
    from .llm.mock import ScriptedMockLLM
    from .llm.openai_compat import client_from_env

    samples_file = samples_path(name, presets_root)
    with open(samples_file, "r", encoding="utf-8") as fh:
        samples = yaml.safe_load(fh) or {}
    cases = []
    for i, sample in enumerate(samples.get("inputs") or [], start=1):
        cases.append(GoldenCase(
            case_id=str(sample.get("name") or f"sample-{i}"),
            input=sample["input"],
            expected_conclusion=sample.get("expected"),
        ))
    if include_golden:
        golden_file = presets_dir(presets_root) / name / "golden.yaml"
        if golden_file.is_file():
            with open(golden_file, "r", encoding="utf-8") as fh:
                golden = yaml.safe_load(fh) or {}
            for i, case in enumerate(golden.get("cases") or [], start=1):
                cases.append(GoldenCase(
                    case_id=str(case.get("name") or f"golden-{i}"),
                    input=case["input"],
                    expected_conclusion=case.get("expected_conclusion"),
                ))
    if not cases:
        raise VafError(f"预置领域 '{name}' 无评估用例（samples.expected / golden.yaml）")

    live = client_from_env() is not None or bool(os.environ.get("VAF_EVAL_LIVE"))
    app = VafApp()
    if live:
        client = client_from_env()
        app.register_model_endpoint(client.name, client, tier=1)
    else:
        app.register_model_endpoint(
            "preset-mock",
            ScriptedMockLLM(name="preset-mock", responder=PresetResponder(samples.get("responses") or {})),
            tier=1,
        )
    _load_preset_domain(app, name, presets_root)
    offline_cases = [c for c in cases if c.expected_conclusion is not None]
    dataset_cases = cases if live else [c for c in cases[: len(samples.get("inputs") or [])]]
    from .eval.dataset import GoldenDataset

    dataset = GoldenDataset(domain=name, cases=dataset_cases)
    report = EvalRunner(app.engine).run(
        dataset,
        request_prefix=f"preset-eval:{name}",
        gates=[Gate(metric="conclusion_accuracy", threshold=1.0)],
    )
    # HITL 类预置：expected 以 waiting_ 开头的用例核对终态而非结论
    for case in report.cases:
        sample_expected = next(
            (c.expected_conclusion for c in dataset_cases if c.case_id == case.case_id), None
        )
        if sample_expected and sample_expected in ("succeeded", "failed", "waiting_review", "waiting_input"):
            case.correct = case.status == sample_expected  # 终态语义：核对 status 而非 conclusion
    if any(c.correct is not None for c in report.cases):
        scored = [c for c in report.cases if c.correct is not None]
        report.metrics["conclusion_accuracy"] = round(
            sum(1 for c in scored if c.correct) / len(scored), 4
        )
        report.gate_passed = all(
            gate.passed(report.metrics.get(gate.metric, 0.0))
            for gate in [Gate(metric="conclusion_accuracy", threshold=1.0)]
        )
    return report, ("live" if live else "offline")
