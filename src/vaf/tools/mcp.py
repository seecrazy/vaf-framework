"""MCP（Model Context Protocol）工具接入（V2.10）：stdio JSON-RPC 客户端。

技能可直接调用现成 MCP 服务器（数据库/浏览器/搜索/内部系统工具）——
垂直 Agent 工具生态的事实标准为我所用：

    from vaf.tools.mcp import register_mcp_server
    register_mcp_server(app, name="search", command=["python", "mcp_server.py"])
    # 之后技能 tools: ["search_web_search"]（<server>_<tool> 命名）

协议实现：JSON-RPC 2.0，行分隔 JSON over stdio（MCP stdio transport）；
initialize 握手 → tools/list → 工具注册进 ToolRegistry → tools/call 按需执行。
仅用标准库；服务器子进程随应用生命周期管理。
"""
from __future__ import annotations

import json
import subprocess
import threading
from typing import Any, Dict, List, Optional

from ..errors import ToolError
from ..observe.metrics import default_metrics


class McpStdioClient:
    """单个 MCP 服务器的 stdio 客户端（行分隔 JSON-RPC 2.0）。"""

    def __init__(self, command: List[str], server_name: str, timeout_s: float = 30.0):
        self._name = server_name
        self._timeout = timeout_s
        self._req_id = 0
        self._lock = threading.Lock()
        self._metrics = default_metrics()
        self._calls = self._metrics.counter("mcp_tool_calls_total", "MCP 工具调用次数")
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise ToolError(f"MCP 服务器启动失败（{server_name}）：{exc}") from exc
        self._initialize()

    # ------------------------------------------------------------------
    def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self._req_id += 1
            req_id = self._req_id
            request: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                request["params"] = params
            assert self._proc.stdin and self._proc.stdout
            self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            # 读到匹配 id 的响应（跳过服务器主动通知）
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    raise ToolError(f"MCP 服务器（{self._name}）连接中断")
                message = json.loads(line)
                if message.get("id") == req_id:
                    if "error" in message:
                        raise ToolError(
                            f"MCP 错误（{self._name}.{method}）：{message['error']}"
                        )
                    return message.get("result") or {}

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                message["params"] = params
            assert self._proc.stdin
            self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()

    def _initialize(self) -> None:
        result = self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "vaf-mcp-client", "version": "1.0.0"},
            },
        )
        self._server_info = result.get("serverInfo", {})
        self._notify("notifications/initialized")

    # ------------------------------------------------------------------
    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._send_request("tools/list")
        return result.get("tools") or []

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self._calls.inc(server=self._name, tool=tool_name)
        result = self._send_request(
            "tools/call", {"name": tool_name, "arguments": arguments}
        )
        if result.get("isError"):
            text = _extract_text(result.get("content"))
            raise ToolError(f"MCP 工具执行失败（{self._name}.{tool_name}）：{text[:200]}")
        return {"content": _extract_text(result.get("content")), "raw": result}

    def close(self) -> None:
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self._proc.kill()


def _extract_text(content: Any) -> str:
    if isinstance(content, list):
        parts = [
            item.get("text", "") for item in content if isinstance(item, dict)
        ]
        return "\n".join(p for p in parts if p)
    return str(content or "")


def register_mcp_server(
    app,
    *,
    name: str,
    command: List[str],
    allowed_prefix: Optional[str] = None,
) -> List[str]:
    """启动 MCP 服务器并把其工具注册进 ToolRegistry（命名 <name>_<tool>）。

    返回注册的工具名列表。技能按这些名字声明 tools 白名单即可调用。
    """
    from .registry import ToolSpec

    client = McpStdioClient(command, name)
    registered: List[str] = []
    for tool in client.list_tools():
        tool_name = tool.get("name", "")
        if not tool_name:
            continue
        if allowed_prefix and not tool_name.startswith(allowed_prefix):
            continue
        registered_name = f"{name}_{tool_name}"
        spec = ToolSpec(
            name=registered_name,
            description=tool.get("description", ""),
            params_schema=tool.get("inputSchema") or {},
            idempotent=False,
            has_side_effect=True,  # MCP 工具副作用未知，按保守策略处理
        )
        app.tool_registry.register(
            (lambda c, t: lambda args: c.call_tool(t, args))(client, tool_name),
            spec,
        )
        registered.append(registered_name)
    # 应用关闭钩子：杀掉子进程
    existing_close = getattr(app, "close", None)

    def _close() -> None:
        client.close()
        if existing_close:
            existing_close()

    app.close = _close  # type: ignore[attr-defined]
    return registered
