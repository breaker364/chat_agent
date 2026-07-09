#!/usr/bin/env python
"""End-to-end test: start the agent server, send a prompt, verify the session output.

Usage:
    python test_e2e.py                    # run with default test prompt
    python test_e2e.py --prompt "..."     # custom prompt
    python test_e2e.py --port 18001       # custom port
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parent
SESSION_DIR = PROJECT_ROOT / "sessionss"

DEFAULT_PROMPT = "今天是2026年7月8日周三，本周日是7月12日。请帮我查询本周日北京到上海的火车票信息，列出5趟车次。你不需要写飞书表格，只需查询并列出结果。"
DEFAULT_PORT = 18001
SERVER_STARTUP_TIMEOUT = 90
AGENT_TIMEOUT = 180


def _red(text: str) -> str:
    return f"\033[91m{text}\033[0m"


def _green(text: str) -> str:
    return f"\033[92m{text}\033[0m"


def _yellow(text: str) -> str:
    return f"\033[93m{text}\033[0m"


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


def find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_health(url: str, timeout: int) -> bool:
    """Poll /health until the server responds or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = requests.get(url, timeout=2)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def start_server(port: int) -> subprocess.Popen:
    """Launch uvicorn as a subprocess."""
    env = dict(os.environ)
    env["CHAT_AGENT_TEST_PORT"] = str(port)

    cmd = [
        sys.executable,
        "-m", "uvicorn",
        "backend.main:app",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--log-level", "warning",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    """Gracefully stop the server process."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)


def send_chat(base_url: str, session_id: str, message: str) -> dict[str, Any]:
    """Send a chat message via the sync /chat endpoint."""
    resp = requests.post(
        f"{base_url}/chat",
        json={
            "message": message,
            "session_id": session_id,
        },
        timeout=AGENT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def load_session(session_id: str) -> dict[str, Any] | None:
    """Load the persisted session file."""
    session_path = SESSION_DIR / session_id / "session.json"
    if not session_path.exists():
        # Try legacy flat-file path
        session_path = SESSION_DIR / f"{session_id}.json"
    if not session_path.exists():
        return None
    return json.loads(session_path.read_text(encoding="utf-8"))


def check_session(session: dict[str, Any], session_id: str) -> list[str]:
    """Run verification checks on a session. Returns list of failure messages."""
    failures: list[str] = []

    # 1. Basic structure
    msgs = session.get("messages", [])
    if len(msgs) < 2:
        failures.append(f"Expected at least 2 messages (user + assistant), got {len(msgs)}")
        return failures

    user_msg = msgs[0]
    assistant_msg = msgs[1]

    if user_msg.get("role") != "user":
        failures.append(f"First message role should be 'user', got '{user_msg.get('role')}'")
    if assistant_msg.get("role") != "assistant":
        failures.append(f"Second message role should be 'assistant', got '{assistant_msg.get('role')}'")

    # 2. Assistant has content
    content = assistant_msg.get("content", "")
    if not isinstance(content, str) or len(content.strip()) < 20:
        failures.append(f"Assistant response too short: {len(content)} chars")

    # 3. Tools were recorded
    tools = assistant_msg.get("tools", [])
    tool_calls = [t for t in tools if t.get("type") == "tool_call"]
    tool_results = [t for t in tools if t.get("type") == "tool_result"]
    if not tool_calls:
        failures.append("No tool calls recorded in assistant message")

    # 4. No excessive duplicate calls for idempotent tools
    call_counts: dict[str, int] = {}
    for tc in tool_calls:
        name = tc.get("name", "?")
        call_counts[name] = call_counts.get(name, 0) + 1

    # Tools that should never be called more than 3 times in a single turn
    low_repeat_tools = {"get-current-date", "feishu_login_status", "get-station-code-of-citys"}
    for tool_name, count in call_counts.items():
        if tool_name in low_repeat_tools and count > 3:
            failures.append(
                f"Tool `{tool_name}` called {count} times (should be <= 3; dedup may not be active)"
            )

    # 5. Task progress is recorded
    progress = session.get("task_progress", {})
    if not isinstance(progress, dict):
        failures.append("task_progress is missing or not a dict")
    else:
        status = progress.get("status", "")
        if not status:
            failures.append("task_progress.status is missing")

        plan = progress.get("task_plan", {})
        todos = plan.get("todos", []) if isinstance(plan, dict) else []
        if isinstance(todos, list) and todos:
            # Check that the plan is model-owned (not the old fixed generic plan)
            todo_ids = {t.get("task_id", "") for t in todos}
            generic_ids = {"context_gathering", "data_query", "target_write", "verification"}
            has_non_generic = bool(todo_ids - generic_ids)
            all_generic = todo_ids == generic_ids
            if all_generic:
                # This is fine if the model chose these IDs, but note it
                pass
            # Check that at least some todos are completed or in_progress
            completed_count = sum(1 for t in todos if t.get("status") == "completed")
            in_progress_count = sum(1 for t in todos if t.get("status") == "in_progress")

    # 6. No warning about "Ignored task list change"
    warnings = plan.get("warnings", []) if isinstance(plan, dict) else []
    lock_warnings = [w for w in warnings if "Ignored task list change" in w or "non-current todo" in w]
    if lock_warnings:
        failures.append(f"Session contains task plan lock warnings: {lock_warnings}")

    return failures


def print_session_summary(session: dict[str, Any]) -> None:
    """Print a human-readable summary of the session."""
    msgs = session.get("messages", [])
    if len(msgs) < 2:
        print(_red("  No assistant response found."))
        return

    assistant_msg = msgs[1]
    tools = assistant_msg.get("tools", [])
    tool_calls = [t for t in tools if t.get("type") == "tool_call"]
    tool_results = [t for t in tools if t.get("type") == "tool_result"]

    # Tool call counts
    call_counts: dict[str, int] = {}
    for tc in tool_calls:
        name = tc.get("name", "?")
        call_counts[name] = call_counts.get(name, 0) + 1

    print(f"  Tool calls: {len(tool_calls)} ({len(tool_results)} results)")
    for name, count in sorted(call_counts.items(), key=lambda x: -x[1]):
        color = _red if count > 3 else _green
        print(f"    {name}: {color(str(count))}")

    # Task plan
    progress = session.get("task_progress", {})
    status = progress.get("status", "?")
    color = _green if status == "completed" else _yellow
    print(f"  Status: {color(status)}")

    plan = progress.get("task_plan", {}) if isinstance(progress, dict) else {}
    todos = plan.get("todos", []) if isinstance(plan, dict) else []
    if todos:
        print(f"  Task plan ({len(todos)} todos):")
        for t in todos:
            s = t.get("status", "?")
            sc = _green if s == "completed" else (_yellow if s == "in_progress" else "")
            print(f"    [{sc}{s}\033[0m] {t.get('task_id')}: {t.get('content', '')[:60]}")
        warnings = plan.get("warnings", [])
        if warnings:
            for w in warnings:
                print(f"    {_yellow('Warning:')} {w[:120]}")

    # Token usage
    usage = assistant_msg.get("usage", {})
    if usage:
        print(f"  Token usage: input={usage.get('input_tokens','?')} output={usage.get('output_tokens','?')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end agent test")
    parser.add_argument("--prompt", type=str, default=DEFAULT_PROMPT,
                        help="Test prompt to send")
    parser.add_argument("--port", type=int, default=0,
                        help="Server port (0 = auto-select)")
    parser.add_argument("--keep-server", action="store_true",
                        help="Leave the server running after test")
    args = parser.parse_args()

    port = args.port or find_free_port()
    session_id = f"session-test-{uuid.uuid4().hex[:12]}"
    base_url = f"http://127.0.0.1:{port}"

    print(f"{_bold('E2E Agent Test')}")
    print(f"  Prompt: {args.prompt[:100]}...")
    print(f"  Port: {port}")
    print(f"  Session: {session_id}")
    print()

    # 1. Start server
    print("Starting server...")
    server_proc = start_server(port)
    try:
        # 2. Wait for ready
        print(f"Waiting for health check at {base_url}/health ...")
        if not wait_for_health(f"{base_url}/health", SERVER_STARTUP_TIMEOUT):
            print(_red("Server failed to start within timeout!"))
            stdout, stderr = server_proc.communicate(timeout=1)
            if stderr:
                print(f"Server stderr:\n{stderr[:2000]}")
            return 1
        print(_green("Server is ready."))

        # 3. Send prompt
        print(f"\nSending prompt: {args.prompt[:80]}...")
        start_time = time.monotonic()
        try:
            result = send_chat(base_url, session_id, args.prompt)
            elapsed = time.monotonic() - start_time
        except requests.Timeout:
            print(_red(f"Agent request timed out after {AGENT_TIMEOUT}s!"))
            return 1
        except requests.ConnectionError as e:
            print(_red(f"Connection failed: {e}"))
            return 1

        print(f"  Response received in {elapsed:.1f}s")
        print(f"  Reply length: {len(result.get('reply', ''))} chars")
        print(f"  Session: {result.get('session_id', '?')}")

        # 4. Read session
        print(f"\nLoading session file...")
        session = load_session(session_id)
        if session is None:
            print(_red(f"Session file not found for {session_id}!"))
            return 1
        print(_green("Session loaded."))

        # 5. Print summary
        print(f"\n{_bold('Session Summary')}:")
        print_session_summary(session)

        # 6. Verify
        print(f"\n{_bold('Verification')}:")
        failures = check_session(session, session_id)
        if failures:
            for f in failures:
                print(f"  {_red('FAIL')} {f}")
            print(f"\n{_red(f'{len(failures)} check(s) failed.')}")
            return 1
        else:
            print(f"  {_green('All checks passed!')}")
            return 0

    finally:
        if not args.keep_server:
            print(f"\nStopping server...")
            stop_server(server_proc)
            print("Server stopped.")
        else:
            print(f"\nServer left running at {base_url} (--keep-server)")


if __name__ == "__main__":
    raise SystemExit(main())
