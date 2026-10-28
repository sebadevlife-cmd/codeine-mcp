"""Manual stdio smoke client: speaks JSON-RPC with codeine-mcp over pipes."""
import json
import subprocess
import sys
from pathlib import Path

EXE = Path.home() / "AppData/Roaming/Python/Python313/Scripts/codeine-mcp.exe"
WS = "C:/Users/Sebastian/Documents/deepseek-harness/default-workspace/codeine-mcp"


def main() -> int:
    proc = subprocess.Popen([str(EXE)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8")

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def read_until(want_id):
        while True:
            line = proc.stdout.readline()
            if not line:
                return None
            msg = json.loads(line)
            if msg.get("id") == want_id:
                return msg

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "smoke", "version": "0.0.1"}}})
    hello = read_until(1)
    print("server :", hello["result"]["serverInfo"])

    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
          "params": {"name": "search_symbols",
                     "arguments": {"query": "skeleton", "workspace_root": WS}}})
    resp = read_until(2)
    text = resp["result"]["content"][0]["text"]
    print("search :", text.splitlines()[0])
    for line in text.splitlines()[1:4]:
        print("        ", line)

    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
          "params": {"name": "get_file_skeleton",
                     "arguments": {"file_path": "src/codeine/indexer.py",
                                   "workspace_root": WS}}})
    resp = read_until(3)
    text = resp["result"]["content"][0]["text"]
    lines = text.splitlines()
    print("skeleton:", lines[0], f"({len(lines)} lines, bodies collapsed:",
          sum("[body:" in l for l in lines), ")")

    proc.stdin.close()
    proc.wait(timeout=10)
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
