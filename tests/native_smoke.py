"""No paid calls: native Copilot CLI -> CLM -> scripted provider.

The scripted provider asks Copilot's real bash tool to produce then edit a
sentinel. Assertions inspect the next provider request and the native transcript.
Run: PYTHONPATH=. python3 tests/native_smoke.py
"""
import json
import argparse
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Provider(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        step = len(type(self).requests)
        Path(self.server.root, f"provider-request-{step}.json").write_text(json.dumps(body, indent=2))
        tools = body.get("tools", [])
        chat = self.path.endswith("/chat/completions")
        if step < 3:
            bash = next(t.get("function", t) for t in tools if t.get("function", t).get("name") == "bash")
            if step == 1:
                command = "printf 'CLM_OLD_SENTINEL_7a48\\n'"
            else:
                text = json.dumps(body)
                mirror = re.search(r"(/[^\s\"\\]+/context\.json)", text).group(1)
                script = ("import json; from pathlib import Path; "
                          f"p=Path({mirror!r}); d=json.loads(p.read_text()); "
                          "rows=[m for m in d['messages'] if m['editable'] and 'CLM_OLD_SENTINEL_7a48' in m['text']]; "
                          "assert rows, 'no editable sentinel'; "
                          "[m.update(text='CLM_KEPT_FACT_84b2') for m in rows]; "
                          "p.write_text(json.dumps(d)); print('edited')")
                command = "python3 - <<'CLM_PY'\n" + script + "\nCLM_PY"
            item = {"id": f"fc_{step}", "type": "function_call", "call_id": f"call_{step}",
                    "name": bash["name"], "arguments": json.dumps({"command": command,
                     "description": "Verify CLM mirror projection", "initial_wait": 1000}), "status": "completed"}
        else:
            item = {"id": "msg_final", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "CLM_SMOKE_OK", "annotations": []}]}
        response = {"id": f"resp_{step}", "object": "response", "created_at": 1, "model": body["model"],
                    "status": "completed", "output": [item], "usage": {
                        "input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}
        if chat:
            assert body.get("stream"), "Native Chat Completions must stream"
            chunk = {"id": f"chatcmpl_{step}", "object": "chat.completion.chunk",
                     "created": 1, "model": body["model"]}
            delta = {"role": "assistant"}
            if step < 3:
                delta["tool_calls"] = [{"index": 0, "id": item["call_id"], "type": "function",
                                        "function": {"name": item["name"], "arguments": item["arguments"]}}]
            else:
                delta["content"] = "CLM_SMOKE_OK"
            chunks = [{**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {**chunk, "choices": [{"index": 0, "delta": {},
                        "finish_reason": "tool_calls" if step < 3 else "stop"}]},
                      {**chunk, "choices": [], "usage": {"prompt_tokens": 100,
                                                        "completion_tokens": 20, "total_tokens": 120}}]
            data = ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
        elif body.get("stream"):
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, **({"arguments": ""} if step < 3 else {"content": []})}},
            ]
            if step < 3:
                events += [{"type": "response.function_call_arguments.delta", "item_id": item["id"],
                            "output_index": 0, "delta": item["arguments"]},
                           {"type": "response.function_call_arguments.done", "item_id": item["id"],
                            "output_index": 0, "arguments": item["arguments"]}]
            else:
                events += [{"type": "response.content_part.added", "item_id": item["id"], "output_index": 0,
                            "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
                           {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
                            "content_index": 0, "delta": "CLM_SMOKE_OK"},
                           {"type": "response.output_text.done", "item_id": item["id"], "output_index": 0,
                            "content_index": 0, "text": "CLM_SMOKE_OK"}]
            events += [{"type": "response.output_item.done", "output_index": 0, "item": item},
                       {"type": "response.completed", "response": response}]
            data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        else:
            data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("--wire-api", choices=["responses", "completions"], default="responses")
    args = parser.parse_args()
    key = "input" if args.wire_api == "responses" else "messages"

    def outputs(body):
        return [m.get("output", m.get("content")) for m in body[key]
                if m.get("type") == "function_call_output" or m.get("role") == "tool"]

    with tempfile.TemporaryDirectory(prefix="clm-native-test-") as temp:
        root = Path(temp)
        if args.output:
            root = args.output.absolute()
            root.mkdir(parents=True, exist_ok=False)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        server.root = root
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {**os.environ, "COPILOT_HOME": str(root / "copilot-home"), "COPILOT_OFFLINE": "true",
               "COPILOT_AUTO_UPDATE": "false", "COPILOT_DISABLE_LOGIN_SHELL_ENV": "true",
               "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        work = root / "workspace"
        work.mkdir(exist_ok=True)
        try:
            session_id = str(uuid.uuid4())
            command = [sys.executable, "-m", "copilot_clm", "run", "--upstream",
                f"http://127.0.0.1:{server.server_port}/v1", "--model", "clm-test", "--budget", "128k",
                "--wire-api", args.wire_api,
                "--state-dir", str(root / "clm-state"), "--record-requests", "--",
                "--disable-builtin-mcps", "--disable-client-telemetry", "--allow-all-tools",
                "--available-tools", "bash", "--session-id", session_id]
            result = subprocess.run([*command, "-p", "Run the requested CLM integration checks."],
                env=env, cwd=work, capture_output=True, text=True, timeout=90)
            (root / "cli-output.txt").write_text(result.stdout + result.stderr)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "CLM_SMOKE_OK" in result.stdout, result.stdout + result.stderr
            assert len(Provider.requests) == 3, f"Expected 3 requests, got {len(Provider.requests)}"
            edited_outputs = outputs(Provider.requests[2])
            assert any("CLM_KEPT_FACT_84b2" in str(m) for m in edited_outputs), edited_outputs
            assert not any("CLM_OLD_SENTINEL_7a48" in str(m) for m in edited_outputs), edited_outputs
            status = [json.loads(p.read_text()) for p in (root / "clm-state").glob("stream-*/status.json")]
            assert len(status) == 1 and status[0]["revision"] == 1, status
            transcripts = list((root / "copilot-home").rglob("events.jsonl"))
            assert any("CLM_OLD_SENTINEL_7a48" in p.read_text() for p in transcripts), transcripts
            resumed = subprocess.run([*command, "-p", "Continue this session and confirm the retained fact."],
                                     env=env, cwd=work, capture_output=True, text=True, timeout=90)
            (root / "cli-resume-output.txt").write_text(resumed.stdout + resumed.stderr)
            assert resumed.returncode == 0, resumed.stdout + resumed.stderr
            assert len(Provider.requests) == 4, len(Provider.requests)
            assert "CLM_KEPT_FACT_84b2" in outputs(Provider.requests[3])
            statuses = [json.loads(p.read_text()) for p in (root / "clm-state").glob("stream-*/status.json")]
            assert len(statuses) == 1 and statuses[0]["revision"] == 1, statuses
            report = {"result": "PASS", "wire_api": args.wire_api,
                      "cli_version": subprocess.check_output(["copilot", "--version"], text=True).splitlines()[0],
                      "provider_requests": 4, "accepted_edits": 1,
                      "native_transcript_preserved": True, "native_resume_preserved_projection": True,
                      "evidence": str(root)}
            (root / "result.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
