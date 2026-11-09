"""领域包自动发现（DESIGN §17 / V2.1 P2-1）。

两条发现路径：
1. 目录扫描：scan_domain_dirs(paths) —— 目录下每个含 register(app) 函数的包；
2. entry_points：load_entrypoint_domains(group="vaf.domains") —— 第三方发行版注册。

register_discovered(app, dirs) 把两者结果统一注册，返回领域名列表。
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import sys
from types import ModuleType
from typing import Callable, List, Optional

from ..errors import VafError

ENTRY_POINT_GROUP = "vaf.domains"


def _has_register(module: ModuleType) -> bool:
    fn = getattr(module, "register", None)
    return inspect.isfunction(fn) or inspect.isbuiltin(fn)


def scan_domain_dirs(paths: List[str]) -> List[ModuleType]:
    """扫描目录下的领域包（含 __init__.py 且导出 register(app)）。"""
    found: List[ModuleType] = []
    for path in paths:
        if not path or not os.path.isdir(path):
            continue
        if path not in sys.path:
            sys.path.insert(0, path)
        for entry in sorted(os.listdir(path)):
            pkg_init = os.path.join(path, entry, "__init__.py")
            if not os.path.isfile(pkg_init):
                continue
            module = importlib.import_module(entry)
            if _has_register(module):
                found.append(module)
    return found


def load_entrypoint_domains(group: str = ENTRY_POINT_GROUP) -> List[ModuleType]:
    """加载通过 entry_points 注册的第三方领域（pip 安装即发现）。"""
    found: List[ModuleType] = []
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - py<3.10
        return found
    eps = entry_points()
    selected = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, [])
    for ep in selected:
        try:
            obj = ep.load()
            module = obj if isinstance(obj, ModuleType) else inspect.getmodule(obj)
            if module is not None and _has_register(module):
                found.append(module)
        except Exception:  # noqa: BLE001 - 单个领域加载失败不阻断其余
            continue
    return found


def register_discovered(
    app,
    dirs: Optional[List[str]] = None,
    include_entrypoints: bool = True,
) -> List[str]:
    """发现并注册全部领域，返回注册成功的 pipeline 名列表。"""
    modules: List[ModuleType] = []
    if dirs:
        modules.extend(scan_domain_dirs(dirs))
    if include_entrypoints:
        modules.extend(load_entrypoint_domains())
    registered: List[str] = []
    for module in modules:
        before = set(app.engine._domains.keys())
        try:
            module.register(app)
        except VafError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VafError(
                f"domain package '{module.__name__}' failed to register: {exc}"
            ) from exc
        registered.extend(sorted(set(app.engine._domains.keys()) - before))
    return registered
