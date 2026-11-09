"""Embedding 客户端与向量检索（V2.9）：检索质量从关键词升级到语义。

零代码启用（OpenAI 兼容 /embeddings 端点，火山方舟/OpenAI/各国产均支持）：

    export VAF_EMBEDDING_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
    export VAF_EMBEDDING_API_KEY=xxxx
    export VAF_EMBEDDING_MODEL=ep-xxxx-embedding

启用后 lowcode 的 `{type: retrieval, mode: auto}`（默认）自动走向量检索：
规则库条目与查询分别向量化，余弦相似度取 top-k；条目向量懒加载并缓存。
未配置时回退关键词检索（BM25-lite），行为与 V2.8 一致。
"""
from __future__ import annotations

import json
import math
import os
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from ..errors import VafError
from ..observe.metrics import default_metrics

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class EmbeddingClient:
    """OpenAI 兼容 /embeddings 客户端（纯标准库）。"""

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout_s: float = 30.0):
        self._url = base_url.rstrip("/") + "/embeddings"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout_s
        self._metrics = default_metrics()
        self._calls = self._metrics.counter("embedding_calls_total", "embedding 调用次数")

    def embed(self, texts: List[str]) -> List[List[float]]:
        self._calls.inc()
        body = json.dumps({"model": self._model, "input": texts}, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(self._url, data=body, headers=headers, method="POST")
        try:
            with _opener.open(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise VafError(f"Embedding 服务返回 HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise VafError(f"Embedding 请求失败（{self._url}）：{exc}") from exc
        ordered = sorted(data["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in ordered]


def embedding_client_from_env(env: Optional[Dict[str, str]] = None) -> Optional[EmbeddingClient]:
    source = env if env is not None else os.environ
    base_url = source.get("VAF_EMBEDDING_BASE_URL", "").strip()
    model = source.get("VAF_EMBEDDING_MODEL", "").strip()
    if not base_url or not model:
        return None
    return EmbeddingClient(base_url, model, source.get("VAF_EMBEDDING_API_KEY", "").strip())


def cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbeddingRetriever:
    """规则库向量检索器：条目向量懒加载缓存，查询向量按需计算。

    构造后传入 RetrievalNode(retriever=...)（Python 路径），或由
    make_auto_retriever 在配置了 VAF_EMBEDDING_* 时自动启用。
    """

    def __init__(self, client: EmbeddingClient, entries_text: List[str], batch_size: int = 16):
        self._client = client
        self._texts = list(entries_text)
        self._batch = batch_size
        self._vectors: Optional[List[List[float]]] = None

    def _ensure_vectors(self):
        if self._vectors is None:
            vectors: List[List[float]] = []
            for i in range(0, len(self._texts), self._batch):
                vectors.extend(self._client.embed(self._texts[i : i + self._batch]))
            self._vectors = vectors

    def top_k(self, query: str, k: int) -> List[str]:
        self._ensure_vectors()
        query_vec = self._client.embed([query])[0]
        scored = [
            (cosine_similarity(query_vec, vec), text)
            for vec, text in zip(self._vectors, self._texts)
        ]
        scored.sort(key=lambda pair: -pair[0])
        return [text for score, text in scored[:k] if score > 0]


def make_auto_retriever(
    entries_text: List[str],
    *,
    client: Optional[EmbeddingClient] = None,
    env: Optional[Dict[str, str]] = None,
):
    """auto 模式解析：配置了 embedding 环境变量 → 向量检索；否则 None（回退关键词）。"""
    embed = client or embedding_client_from_env(env)
    if embed is None:
        return None
    return EmbeddingRetriever(embed, entries_text)
