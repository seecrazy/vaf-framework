"""假 MCP 服务器（stdio JSON-RPC）：两个工具 echo / add，用于集成测试。"""
import json
import sys

TOOLS = [
    {"name": "echo", "description": "回显文本", "inputSchema": {
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "两数相加", "inputSchema": {
        "type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
]

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if "id" not in msg:  # 通知
        continue
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05",
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-mcp", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg["params"]
        if params["name"] == "echo":
            content = [{"type": "text", "text": f"echo:{params['arguments']['text']}"}]
        elif params["name"] == "add":
            a, b = params["arguments"]["a"], params["arguments"]["b"]
            content = [{"type": "text", "text": str(a + b)}]
        else:
            content = [{"type": "text", "text": "unknown tool"}]
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                              "result": {"content": content, "isError": True}}), flush=True)
            continue
        result = {"content": content, "isError": False}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
