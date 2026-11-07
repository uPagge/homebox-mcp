"""Bearer-mode protocol health only; does not read or mutate inventory."""
import json
import os
import urllib.request


def main():
    version = "2026-07-28"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {"name": "healthcheck", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }}}
    request = urllib.request.Request("http://127.0.0.1:8000/mcp",
        data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": version, "Mcp-Method": "tools/list",
            "Authorization": "Bearer " + os.environ["HOMEBOX_MCP_TOKEN"],
        })
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.load(response)
    if "error" in result or "tools" not in result.get("result", {}):
        raise SystemExit("MCP protocol health check failed")


if __name__ == "__main__":
    main()
