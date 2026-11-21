"""Explicit opt-in paid smoke with a real model and the native CLI.

PYTHONPATH=. python3 tests/live_smoke.py /absolute/new/output/directory
Uses the configured local Copilot gateway; no provider keys are saved.
"""
import json
import argparse
import os
import subprocess
import sys
from pathlib import Path


PROMPT = """This is an integration test of CLM in the native Copilot CLI.
Use bash to run `cat fixture.txt`; it contains a fact and obsolete verbose log.
Then read the CLM context.json mirror described in your system instructions.
Using Python via bash, replace ONLY the editable tool result containing the long
fixture log with exactly `CLM_LIVE_COMPACTED: deployment code is ORCHID-7391`.
Preserve its id/metadata and all other entries and header. Do not edit the call
arguments. Execute that edit, then in a separate tool call read status.json next
to context.json and check that revision is at least 1 and outcome is accepted edit.
If rejected, fix the mirror and retry once. Finally reply CLM_LIVE_OK and the
deployment code. Do not edit any state or control files, and do not run subagents.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", default=os.environ.get("COPILOT_MODEL", "gpt-6-astra"))
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o700)
    work = root / "workspace"
    work.mkdir()
    (work / "fixture.txt").write_text("deployment code is ORCHID-7391\n" +
                                     "CLM_LIVE_OBSOLETE_LOG unneeded historical output\n" * 100)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "COPILOT_HOME": str(root / "copilot-home"), "COPILOT_OFFLINE": "true",
           "COPILOT_AUTO_UPDATE": "false", "COPILOT_DISABLE_LOGIN_SHELL_ENV": "true",
           "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS": "4096"}
    command = [sys.executable, "-m", "copilot_clm", "run", "--model", args.model,
               "--state-dir", str(root / "clm-state"), "--record-requests", "--",
               "--disable-builtin-mcps", "--disable-client-telemetry", "--allow-all-tools",
               "--trust-folder", "--available-tools", "bash", "--reasoning-effort", "low", "-p", PROMPT]
    result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=300)
    (root / "cli-output.txt").write_text(result.stdout + result.stderr)
    statuses = [json.loads(p.read_text()) for p in (root / "clm-state").glob("stream-*/status.json")]
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLM_LIVE_OK" in result.stdout and "ORCHID-7391" in result.stdout, result.stdout
    assert len(statuses) == 1 and statuses[0]["revision"] >= 1, statuses
    stream = root / "clm-state" / statuses[0]["stream"]
    requests = [json.loads(p.read_text()) for p in sorted((stream / "requests").glob("*.json"))]
    compacted = [b for b in requests if any(
        m.get("type") == "function_call_output" and m.get("output") ==
        "CLM_LIVE_COMPACTED: deployment code is ORCHID-7391" for m in b["input"])]
    assert compacted, "No provider request contains the edited result"
    for body in compacted:
        assert not any(m.get("type") == "function_call_output" and
                       "CLM_LIVE_OBSOLETE_LOG" in str(m.get("output", "")) and
                       str(m.get("output", "")).count("CLM_LIVE_OBSOLETE_LOG") > 50 for m in body["input"])
    report = {"result": "PASS", "cli_version": subprocess.check_output(["copilot", "--version"], text=True).splitlines()[0],
              "model": args.model, "requests": len(requests), "accepted_edits": statuses[0]["revision"],
              "requests_with_edited_output": len(compacted), "state": str(stream),
              "usage": [json.loads(p.read_text()) for p in sorted((stream / "usage").glob("*.json"))]}
    (root / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
