"""Launch the native Copilot executable with a process-scoped CLM provider."""
import argparse
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from . import __version__
from .engine import Engine
from .proxy import Proxy
from .storage import encode, private_dir, read_json, write_json


def positive_int(value):
    number = int(value[:-1]) * 1000 if value.lower().endswith("k") else int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be positive")
    return number


def parser():
    p = argparse.ArgumentParser(description="CLM context editing for the native Copilot CLI")
    p.add_argument("--version", action="version", version=__version__)
    subs = p.add_subparsers(dest="command", required=True)
    run = subs.add_parser("run", help="Start Copilot with editable context")
    run.add_argument("--upstream", default=os.environ.get("CLM_UPSTREAM_URL") or os.environ.get("COPILOT_BASE_URL")
                     or os.environ.get("COPILOT_PROVIDER_BASE_URL") or "http://127.0.0.1:4141/v1")
    run.add_argument("--model", default=os.environ.get("COPILOT_MODEL", "gpt-6-astra"))
    run.add_argument("--wire-api", choices=["responses", "completions"], default="responses")
    run.add_argument("--budget", type=positive_int, default=64000)
    run.add_argument("--state-dir", type=Path)
    run.add_argument("--copilot", default="copilot")
    run.add_argument("--no-guard", action="store_true")
    run.add_argument("--record-requests", action="store_true")
    run.add_argument("args", nargs=argparse.REMAINDER, help="Native Copilot options after --")
    for command in ("status", "path", "on", "off", "reset"):
        sub = subs.add_parser(command)
        sub.add_argument("--state-dir", type=Path, default=os.environ.get("CLM_STATE_DIR"))
    return p


def child_environment(args, root, port, token):
    env = dict(os.environ)
    # Remove provider settings that can bypass the loopback projection or replace
    # its token. Never change the user's persistent Copilot config.
    for key in list(env):
        if key.startswith("COPILOT_PROVIDER_") or key in ("COPILOT_PROVIDERS_CONFIG", "CLM_API_KEY", "COPILOT_API_KEY"):
            del env[key]
    env.update(COPILOT_PROVIDER_BASE_URL=f"http://127.0.0.1:{port}/v1",
               COPILOT_PROVIDER_TYPE="openai", COPILOT_PROVIDER_API_KEY=token,
               COPILOT_PROVIDER_WIRE_API=args.wire_api, COPILOT_PROVIDER_TRANSPORT="http",
               COPILOT_MODEL=args.model, CLM_STATE_DIR=str(root))
    # Preserve explicit model limits, which affect native compaction thresholds.
    for key in ("COPILOT_PROVIDER_MAX_PROMPT_TOKENS", "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS",
                "COPILOT_PROVIDER_VISION"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def run(args):
    executable = shutil.which(args.copilot)
    if executable is None:
        raise ValueError("Copilot CLI executable not found; install it or use --copilot")
    if os.name != "posix":
        raise ValueError("This release supports Linux/macOS; native Windows is not yet tested")
    import fcntl
    root = args.state_dir or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "copilot-clm" / uuid.uuid4().hex
    root = private_dir(root.absolute())
    lock_fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("This state directory is already in use by another launcher") from None
        engine = Engine(root, args.budget, not args.no_guard, args.record_requests)
        token = secrets.token_urlsafe(32)
        api_key = os.environ.get("CLM_API_KEY") or os.environ.get("COPILOT_API_KEY") or os.environ.get("COPILOT_PROVIDER_API_KEY", "")
        server = Proxy(engine, args.upstream, token, api_key)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        native_args = args.args[1:] if args.args[:1] == ["--"] else args.args
        command = [executable, "--model", args.model, "--add-dir", str(root), *native_args]
        print(f"CLM {__version__} · state: {root} · budget: ~{args.budget} tokens", file=sys.stderr)
        child = None
        stop_signal = signal.SIGINT

        def stop(signum, _frame):
            nonlocal stop_signal
            stop_signal = signum
            raise KeyboardInterrupt

        previous_term = signal.signal(signal.SIGTERM, stop)
        try:
            child = subprocess.Popen(command, env=child_environment(args, root, server.server_port, token))
            try:
                return child.wait()
            except KeyboardInterrupt:
                # Also handle signals addressed only to the launcher process.
                child.send_signal(stop_signal)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return 128 + stop_signal
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            server.shutdown()
            server.server_close()


def control(args):
    if not args.state_dir:
        raise ValueError("Specify --state-dir from the launcher banner (or CLM_STATE_DIR)")
    root = Path(args.state_dir).absolute()
    if not (root / "control.json").is_file():
        raise ValueError("No CLM state at that location")
    if args.command in ("on", "off", "reset"):
        data = read_json(root / "control.json")
        if args.command == "reset":
            data["reset"] = data.get("reset", 0) + 1
        else:
            data["enabled"] = args.command == "on"
        write_json(root / "control.json", data)
        print("Applied at the next request: " + args.command)
    else:
        for path in sorted(root.glob("stream-*/status.json")):
            data = read_json(path)
            print(data["context_path"] if args.command == "path" else encode(data))


def main():
    args = parser().parse_args()
    try:
        return run(args) if args.command == "run" else control(args) or 0
    except (ValueError, OSError) as exc:
        print("copilot-clm: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
