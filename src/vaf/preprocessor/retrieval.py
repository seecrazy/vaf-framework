"""检索节点（V2.8）：声明式规则库 + BM25-lite 关键词检索（RAG 入门层）。

规则库 YAML 格式（领域包同级 `<name>.knowledge.yaml`）：

    entries:
      - id: dti_rule
        keywords: [dti, 还款收入比, 负债]
        text: |
          DTI ≥ 0.50 拒绝；0.40~0.50 转人工并给降额建议（审慎线 50%）。

检索算法：关键词命中计数（BM25-lite），零外部依赖；生产可注入向量检索
retriever（构造参数 retriever=Callable[[query, top_k], list[str]]）。
命中条目写入 enriched_data[output_field]（默认 knowledge），技能模板用
{{ enriched.knowledge }} 引用——静态检查单升级为"规则库 + 检索增强"。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from ..errors import VafError
from .nodes import EnrichNode, RawItem


def load_knowledge_base(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        raise VafError(f"规则库文件不存在：{path}")
    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    entries = data.get("entries") or []
    for i, entry in enumerate(entries):
        if not entry.get("text"):
            raise VafError(f"规则库 {path} 第 {i + 1} 条缺少 text")
        entry.setdefault("id", f"rule_{i + 1}")
        entry.setdefault("keywords", [])
    return entries


def keyword_rank(
    entries: List[Dict[str, Any]], query_text: str, top_k: int
) -> List[Dict[str, Any]]:
    """BM25-lite：关键词命中计数排序（含 text 自身词元匹配加权）。"""
    query = query_text.lower()
    scored = []
    for entry in entries:
        score = 0.0
        for kw in entry.get("keywords", []):
            if str(kw).lower() in query:
                score += 2.0
        text = entry["text"].lower()
        if text[:60] in query or query[:60] in text:
            score += 0.5
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda pair: -pair[0])
    return [entry for _, entry in scored[:top_k]]


class RetrievalNode(EnrichNode):
    """从规则库检索与输入相关的条目，写入 enriched（供提示词引用）。"""

    name = "retrieval"
    fail_strategy = "skip"

    def __init__(
        self,
        path: str,
        *,
        top_k: int = 3,
        output_field: str = "knowledge",
        query_fields: Optional[List[str]] = None,
        retriever: Optional[Callable[[str, int], List[str]]] = None,
        mode: str = "auto",
    ):
        self._path = path
        self.top_k = top_k
        self.output_field = output_field
        self.query_fields = query_fields
        self._explicit_retriever = retriever
        self._mode = mode  # keyword=强制关键词；auto=配置 embedding 环境变量时用向量
        self._auto_retriever = None
        self._entries: Optional[List[Dict[str, Any]]] = None  # 懒加载一次

    def _load(self) -> List[Dict[str, Any]]:
        if self._entries is None:
            self._entries = load_knowledge_base(self._path)
        return self._entries

    def _resolve_retriever(self):
        """优先级：显式注入 > auto（embedding 环境变量）> 关键词。"""
        if self._explicit_retriever is not None:
            return self._explicit_retriever
        if self._mode == "keyword":
            return None
        if self._auto_retriever is None:
            from ..llm.embedding import make_auto_retriever

            entries = self._load()
            self._auto_retriever = make_auto_retriever([e["text"].strip() for e in entries])
        return self._auto_retriever

    def enrich(self, item: RawItem) -> Dict[str, Any]:
        import json

        retriever = self._resolve_retriever()
        if retriever is not None:
            query = self._query_text(item)
            if hasattr(retriever, "top_k"):  # EmbeddingRetriever 对象
                hits = retriever.top_k(query, self.top_k)
            else:  # Callable[[query, top_k], list]
                hits = retriever(query, self.top_k)
            return {self.output_field: hits}
        entries = self._load()
        if not entries:
            return {}
        hits = keyword_rank(entries, self._query_text(item), self.top_k)
        return {self.output_field: [e["text"].strip() for e in hits]}

    def _query_text(self, item: RawItem) -> str:
        import json

        if self.query_fields:
            parts = [str(item.payload.get(f, "")) for f in self.query_fields]
        else:
            parts = [json.dumps(item.payload, ensure_ascii=False, default=str)]
        return " ".join(p for p in parts if p)
