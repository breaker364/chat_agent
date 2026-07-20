from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


CLI_REQUEST_RE = re.compile(r"^\s*(?:lark|lark_cli)\b", re.IGNORECASE)


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def _load_payload() -> dict[str, Any]:
    raw = sys.stdin.buffer.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _emit_result(value: str) -> None:
    sys.stdout.write(json.dumps({"result": value}, ensure_ascii=False))


def _error_payload(message: str, **extra: Any) -> str:
    payload = {"success": False, "error": message}
    payload.update({key: value for key, value in extra.items() if value not in (None, "")})
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _find_feishu_personal_runner(skill_root: Path) -> Path:
    candidates = [
        skill_root.parent / "feishu-personal" / "skill_runner.py",
    ]
    workspace_root = os.environ.get("CHAT_AGENT_WORKSPACE_ROOT")
    if workspace_root:
        candidates.append(Path(workspace_root) / "skills" / "feishu-personal" / "skill_runner.py")
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError("Could not find sibling feishu-personal/skill_runner.py")


def main() -> int:
    _force_utf8_stdio()
    skill_root = Path(os.environ.get("CHAT_AGENT_SKILL_ROOT") or Path(__file__).resolve().parent).resolve()
    payload = _load_payload()
    request = str(payload.get("request") or "").strip()

    if not CLI_REQUEST_RE.match(request):
        _emit_result(
            _error_payload(
                "feishu-personal-cli accepts only explicit CLI requests beginning with `lark` or `lark_cli`.",
                received=request,
                expected='lark <command> [args]',
            )
        )
        return 0

    try:
        runner = _find_feishu_personal_runner(skill_root)
    except Exception as exc:
        _emit_result(_error_payload(str(exc)))
        return 0

    env = dict(os.environ)
    env["CHAT_AGENT_SKILL_ROOT"] = str(runner.parent)
    env["PYTHONIOENCODING"] = "utf-8"
    workspace_root = os.environ.get("CHAT_AGENT_WORKSPACE_ROOT")
    if workspace_root:
        env["CHAT_AGENT_WORKSPACE_ROOT"] = workspace_root

    process = subprocess.run(
        [sys.executable, str(runner)],
        input=json.dumps({"request": request}, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(runner.parent),
        env=env,
        check=False,
    )
    stdout_text = process.stdout.decode("utf-8", errors="replace").strip()
    stderr_text = process.stderr.decode("utf-8", errors="replace").strip()

    if process.returncode != 0:
        _emit_result(
            _error_payload(
                "Underlying feishu-personal CLI command failed.",
                command=request,
                exit_code=process.returncode,
                stderr=stderr_text,
                stdout=stdout_text,
            )
        )
        return 0

    if not stdout_text:
        _emit_result("")
        return 0

    try:
        parsed = json.loads(stdout_text)
    except json.JSONDecodeError:
        _emit_result(stdout_text)
        return 0

    if isinstance(parsed, dict) and "result" in parsed:
        _emit_result(str(parsed["result"]))
    else:
        _emit_result(json.dumps(parsed, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
