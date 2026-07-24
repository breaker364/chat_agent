"""feishu-personal-cli — standalone CLI-only Feishu/Lark skill.

Executes explicit ``lark ...`` commands in-process through the ``lark_tools``
package bundled locally under ``lark_tools/``.  This skill is fully
self-contained and has no runtime dependency on the sibling ``feishu-personal``
skill.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ensure the local lark_tools/ package is importable.
# ---------------------------------------------------------------------------
_SKILL_DIR = Path(__file__).resolve().parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

import lark_tools.cli as lark_cli  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLI_REQUEST_RE = re.compile(r"^\s*(?:lark|lark_cli)\b", re.IGNORECASE)
LARK_CLI_RE = re.compile(r"(?:^|\n)\s*(?:lark_cli|lark)\s+(.+)$", re.I | re.S)


# ---------------------------------------------------------------------------
# Stdin / stdout helpers
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Session cookie management
# ---------------------------------------------------------------------------
def _load_session_cookie() -> str:
    session_file = _SKILL_DIR.parent.parent / "sessionss" / "feishu_web_session.json"
    if not session_file.exists():
        raise RuntimeError(
            f"Feishu session file not found at {session_file}. "
            "Run Feishu QR login first (use feishu_login_status to check, then trigger login flow)."
        )
    payload = json.loads(session_file.read_text(encoding="utf-8"))
    session = str(payload.get("session") or "").strip()
    if not session:
        raise RuntimeError(
            f"Missing session cookie in {session_file}. "
            "The session file exists but contains no valid session cookie. Run Feishu QR login again."
        )
    return session


def _load_cookies() -> list[dict[str, str]]:
    return [{"name": "session", "value": _load_session_cookie(), "domain": ".feishu.cn"}]


# ---------------------------------------------------------------------------
# Proxy bypass — prevents stale CSRF cookies from leaking across requests
# ---------------------------------------------------------------------------
def _patch_requests_no_proxy() -> None:
    import requests

    def request_no_proxy(method: str, url: str, **kwargs):
        with requests.Session() as session:
            session.trust_env = False
            return session.request(method, url, **kwargs)

    def get_no_proxy(url: str, params=None, **kwargs):
        return request_no_proxy("GET", url, params=params, **kwargs)

    def post_no_proxy(url: str, data=None, json=None, **kwargs):
        return request_no_proxy("POST", url, data=data, json=json, **kwargs)

    requests.get = get_no_proxy  # type: ignore[assignment]
    requests.post = post_no_proxy  # type: ignore[assignment]
    requests.request = request_no_proxy  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# CLI command parsing
# ---------------------------------------------------------------------------
def _split_lark_command(command_text: str) -> list[str]:
    """Split CLI text while tolerating an unclosed final quoted argument.

    Model-generated commands often place long multiline Markdown in
    ``--text "..."``. If the final quote is omitted, shlex rejects the whole
    command. This parser keeps the remainder as one argument and preserves
    ordinary backslashes used in Windows paths.
    """
    argv: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0

    while index < len(command_text):
        char = command_text[index]
        if quote is not None:
            if char == quote:
                quote = None
            elif (
                char == "\\"
                and index + 1 < len(command_text)
                and command_text[index + 1] in {quote, "\\"}
            ):
                index += 1
                current.append(command_text[index])
            else:
                current.append(char)
        elif char in {'"', "'"}:
            quote = char
        elif char == "[":
            depth = 0
            in_json_string = False
            escape = False
            while index < len(command_text):
                json_char = command_text[index]
                current.append(json_char)
                if escape:
                    escape = False
                elif json_char == "\\" and in_json_string:
                    escape = True
                elif json_char == '"':
                    in_json_string = not in_json_string
                elif not in_json_string and json_char == "[":
                    depth += 1
                elif not in_json_string and json_char == "]":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
        elif char.isspace():
            if current:
                argv.append("".join(current))
                current = []
        else:
            current.append(char)
        index += 1

    if current:
        argv.append("".join(current))
    return argv


# ---------------------------------------------------------------------------
# CLI execution
# ---------------------------------------------------------------------------
def _execute_lark_cli(request_text: str) -> str:
    """Execute an explicit ``lark ...`` command through lark_tools.cli in-process."""
    match = LARK_CLI_RE.search(request_text or "")
    if not match:
        raise RuntimeError("No explicit lark/lark_cli command found. Use: lark <command> [args].")

    command_text = match.group(1).strip()
    if not command_text:
        raise RuntimeError("Empty lark command.")

    argv = _split_lark_command(command_text)
    if not argv:
        raise RuntimeError("Empty lark command.")

    blocked_tokens = {";", "&&", "||", "|", ">", ">>", "<"}
    if any(token in blocked_tokens for token in argv):
        raise RuntimeError(
            "Shell operators are not allowed in lark CLI passthrough. "
            "Provide argv-style arguments only."
        )

    cookies = _load_cookies()
    old_argv = sys.argv[:]
    old_cwd = os.getcwd()
    old_loader = getattr(lark_cli, "load_and_auth_cookies", None)
    buffer = io.StringIO()
    try:
        lark_cli.load_and_auth_cookies = lambda: cookies  # type: ignore[assignment]
        sys.argv = ["lark", *argv]
        with contextlib.redirect_stdout(buffer):
            try:
                lark_cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                if code not in (0, None):
                    raise RuntimeError(
                        f"lark CLI exited with code {code}. Output: {buffer.getvalue().strip()}"
                    )
    finally:
        if old_loader is not None:
            lark_cli.load_and_auth_cookies = old_loader  # type: ignore[assignment]
        sys.argv = old_argv
        os.chdir(old_cwd)
    return buffer.getvalue().strip()


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------
def main() -> int:
    _force_utf8_stdio()
    _patch_requests_no_proxy()

    payload = _load_payload()
    request = str(payload.get("request") or "").strip()

    if not CLI_REQUEST_RE.match(request):
        _emit_result(
            _error_payload(
                "feishu-personal-cli accepts only explicit CLI requests "
                "beginning with `lark` or `lark_cli`.",
                received=request,
                expected="lark <command> [args]",
            )
        )
        return 0

    try:
        result = _execute_lark_cli(request)
    except Exception as exc:
        _emit_result(_error_payload(str(exc), command=request))
        return 0

    _emit_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
