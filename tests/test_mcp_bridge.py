import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


def test_stdio_bridge_discovers_tools_and_forwards_only_run_token():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, data, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer test-run-token"
            assert self.path == "/tools"
            self.reply([{"name": "linear_search", "description": "Fixture search", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
                        {"name":"credentials_run","description":"Scoped command","inputSchema":{"type":"object"}}])

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer test-run-token"
            if self.path == '/credentials/materialize':
                args=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert args=={'request_ids':['a'*32]}
                self.reply({'status':'ready','bindings':[{'request_id':'a'*32,'revision':1,'format':'env','env_var':'',
                            'value':json.dumps({'TEST_KEY':'synthetic-stdio-secret'})}]})
                return
            assert self.path == "/tools/call"
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if calls[-1]['name'] == 'skills_save':
                self.reply({'detail':'Skill revision changed. Use expected_revision=2 after reviewing the current skill.'},409)
                return
            self.reply({"issues": [{"identifier": "LIT-123", "title": "Fixture issue"}]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "linear_search", "arguments": {"query": "fixture"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "unknown_method"},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name":"skills_save", "arguments":{}}},
        {"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"credentials_run","arguments":{
            "request_ids":['a'*32],"command":shlex.join([sys.executable,'-c',"import os; print(os.environ['TEST_KEY'])"])}}},
    ]
    try:
        script = Path(__file__).resolve().parents[1] / "sandbox" / "mcp_bridge.py"
        result = subprocess.run([sys.executable, str(script)], input="\n".join(json.dumps(m) for m in messages)+"\n",
                                text=True, capture_output=True, timeout=10,
                                env={"PATH": os.environ["PATH"], "WORKSPACE_BROKER_URL": f"http://127.0.0.1:{server.server_port}", "WORKSPACE_RUN_TOKEN": "test-run-token"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.returncode == 0, result.stderr
    output = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(output) == 6  # notifications never receive responses
    assert output[0]["result"]["protocolVersion"] == "2025-03-26"
    names = {tool["name"] for tool in output[1]["result"]["tools"]}
    assert names == {"credentials_run", "linear_search", "browser_open", "browser_read", "browser_click", "browser_fill", "browser_screenshot", "browser_record_start", "browser_record_stop", "browser_key", "browser_scroll"}
    assert "Fixture issue" in output[2]["result"]["content"][0]["text"]
    assert output[3]["error"]["code"] == -32601
    assert output[4]['result']['isError']
    assert 'expected_revision=2' in output[4]['result']['content'][0]['text']
    executed=json.loads(output[5]['result']['content'][0]['text'])
    assert executed['exit_code']==0 and '[credential redacted]' in executed['output']
    assert 'synthetic-stdio-secret' not in result.stdout and 'synthetic-stdio-secret' not in result.stderr
    assert calls == [{"name": "linear_search", "arguments": {"query": "fixture"}}, {'name':'skills_save','arguments':{}}]
