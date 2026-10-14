"""Minimal stdio MCP server used by the probe test (newline-delimited JSON-RPC)."""
import json
import sys

TOOLS = [{"name": "echo", "description": "Echo the input back.",
          "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
         {"name": "ping", "description": "Return pong.", "inputSchema": {"type": "object"}}]

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "fake", "version": "0"}}
    elif method == "tools/list":
        cursor = (msg.get("params") or {}).get("cursor")
        res = {"tools": TOOLS[1:], "nextCursor": None} if cursor else {"tools": TOOLS[:1], "nextCursor": "p2"}
    else:
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n")
    sys.stdout.flush()
