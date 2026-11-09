"""文档抽取节点（V2.7）：非结构化文档入口 → 结构化文本。

支持三种来源（确定性、零外部依赖）：
- http(s) URL：GET 拉取（直连绕过系统代理）；
- 本地文件路径：直接读取；
- data: URL（data:text/plain;base64,... 或 ;utf8,）：自包含样例/测试友好。

内置处理纯文本类（txt/md/csv/json/log）与 html（剥标签）；PDF/DOCX/扫描件等
二进制格式通过 Python 路径注入自定义 extractor（见文件尾示例）。
"""
from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import unquote, urlparse
from urllib.request import Request, build_opener
from urllib.request import ProxyHandler

from ..preprocessor.nodes import EnrichNode, RawItem

_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml", ""}
_opener = build_opener(ProxyHandler({}))  # 内网/本机直连
_TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


def _strip_html(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", _TAG.sub(" ", text)).strip()


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:1024]


def _looks_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith(("<!doctype html", "<html")) or (
        head.startswith("<") and "</" in text[:2000].lower()
    )


def extract_document_text(source: str, *, max_chars: int = 100_000) -> str:
    """确定性抽取文本；二进制格式抛 ValueError（调用方决定降级或拦截）。"""
    source = (source or "").strip()
    if not source:
        raise ValueError("文档来源为空")

    if source.startswith("data:"):
        head, _, payload = source.partition(",")
        if not payload:
            raise ValueError("data: URL 缺少内容")
        if "base64" in head.lower():
            data = base64.b64decode(payload)
        else:
            data = unquote(payload).encode("utf-8")
        text = data.decode("utf-8", "replace")
        if "text/html" in head.lower() or _looks_html(text):
            text = _strip_html(text)
        return text[:max_chars]

    if source.startswith(("http://", "https://")):
        req = Request(source, headers={"User-Agent": "vaf-document-extract/1.0"})
        with _opener.open(req, timeout=30) as resp:
            data = resp.read(max_chars * 4)
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if _looks_binary(data):
            raise ValueError(
                f"HTTP 来源返回二进制内容（{source[:80]}）；PDF/扫描件请注入自定义 extractor"
            )
        text = data.decode("utf-8", "replace")
        if "html" in content_type:
            text = _strip_html(text)
        return text[:max_chars]

    # 本地文件路径
    path = Path(source)
    if not path.is_file():
        raise ValueError(f"文档文件不存在：{source}")
    if path.suffix.lower() not in _TEXT_SUFFIXES and path.suffix.lower() != ".html":
        raise ValueError(
            f"不支持的本地文件类型 {path.suffix or '(无扩展名)'}（支持 {sorted(_TEXT_SUFFIXES)} 与 .html）；"
            f"PDF/DOCX 请注入自定义 extractor"
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".html":
        text = _strip_html(text)
    return text[:max_chars]


class DocumentExtractNode(EnrichNode):
    """读取输入字段（默认 document_url）中的文档来源，抽取文本写入
    enriched_data[output_field]（默认 document_text），供技能模板
    `{{ enriched.document_text }}` 引用。抽取失败按 fail_strategy 降级
    （默认 fallback：写入空串 + 异常标记，不阻塞流程）。"""

    name = "document_extract"
    fail_strategy = "fallback"
    fallback_value = {}

    def __init__(
        self,
        field: str = "document_url",
        output_field: str = "document_text",
        max_chars: int = 100_000,
        extractor: Optional[Callable[[str], str]] = None,
    ):
        self.field = field
        self.output_field = output_field
        self.max_chars = max_chars
        self._extractor = extractor or (lambda src: extract_document_text(src, max_chars=max_chars))

    def enrich(self, item: RawItem) -> Dict[str, Any]:
        source = item.payload.get(self.field)
        if not source:
            return {}
        return {self.output_field: self._extractor(str(source))}


# ---------------------------------------------------------------------------
# 自定义 extractor 示例（PDF：接 pdftotext/OCR/视觉模型后替换实现）：
#
#     def pdf_extractor(url: str) -> str:
#         ...  # 调用你的解析服务 / Tesseract / 视觉 LLM
#         return text
#
#     node = DocumentExtractNode(field="document_url", extractor=pdf_extractor)
#     pre.enrichers = [node]      # Python 领域：直接装配
#
# 低代码 YAML 声明式暂不支持传函数——PDF 场景请转 Python 路径注册节点，
# 或在领域包 register(app) 中替换内置工厂。
# ---------------------------------------------------------------------------
