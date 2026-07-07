from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import requests


URL_RE = re.compile(r"https://[^\s，。；;]+")
BLOCK_TOKEN_RE = re.compile(
    r"(?:blockToken|whiteboard\s*token|画板\s*token|白板\s*token)\s*[:=：]?\s*([A-Za-z0-9]{20,})",
    re.I,
)
LARK_CLI_RE = re.compile(r"(?:^|\n)\s*(?:lark_cli|lark)\s+(.+)$", re.I | re.S)


def _load_payload() -> dict[str, Any]:
    raw = sys.stdin.buffer.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _load_session_cookie(skill_root: Path) -> str:
    session_file = skill_root.parent.parent / "sessionss" / "feishu_web_session.json"
    payload = json.loads(session_file.read_text(encoding="utf-8"))
    session = str(payload.get("session") or "").strip()
    if not session:
        raise RuntimeError(f"Missing session cookie in {session_file}")
    return session


def _load_cookies(skill_root: Path) -> list[dict[str, str]]:
    return [{"name": "session", "value": _load_session_cookie(skill_root), "domain": ".feishu.cn"}]


def _patch_requests_no_proxy() -> None:
    session = requests.Session()
    session.trust_env = False
    requests.get = session.get  # type: ignore[assignment]
    requests.post = session.post  # type: ignore[assignment]
    requests.request = session.request  # type: ignore[assignment]


def _execute_lark_cli(skill_root: Path, request_text: str) -> str:
    """Execute an explicit `lark ...` command through lark_tools.cli.

    This is the full-power entrypoint for feishu-personal. It runs the Python
    CLI in-process, with shell operators blocked, and forces the CLI to reuse
    this agent's local Feishu Web session instead of keyring login.
    """
    match = LARK_CLI_RE.search(request_text or "")
    if not match:
        raise RuntimeError("No explicit lark/lark_cli command found. Use: lark <command> [args].")

    command_text = match.group(1).strip()
    if not command_text:
        raise RuntimeError("Empty lark command.")

    try:
        argv = shlex.split(command_text, posix=False)
    except ValueError as exc:
        raise RuntimeError(f"Could not parse lark command: {exc}") from exc

    if not argv:
        raise RuntimeError("Empty lark command.")

    # posix=False preserves literal quote characters (e.g. --sheet "name"
    # becomes ['--sheet', '"name"'] instead of ['--sheet', 'name']).
    # Strip surrounding matching single/double quotes from each arg so that
    # downstream consumers (like _select_sheets which matches by name/id)
    # see the clean value.
    argv = [
        a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in ('"', "'") else a
        for a in argv
    ]

    blocked_tokens = {";", "&&", "||", "|", ">", ">>", "<"}
    if any(token in blocked_tokens for token in argv):
        raise RuntimeError("Shell operators are not allowed in lark CLI passthrough. Provide argv-style arguments only.")

    from lark_tools import cli as lark_cli

    cookies = _load_cookies(skill_root)
    old_argv = sys.argv[:]
    old_cwd = os.getcwd()
    old_loader = getattr(lark_cli, "load_and_auth_cookies", None)
    buffer = io.StringIO()
    try:
        # lark_tools.cli normally reads cookies from keyring. The agent stores
        # web login state in sessionss/feishu_web_session.json, so bridge it here.
        lark_cli.load_and_auth_cookies = lambda: cookies  # type: ignore[assignment]
        sys.argv = ["lark", *argv]
        with contextlib.redirect_stdout(buffer):
            try:
                lark_cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                if code not in (0, None):
                    raise RuntimeError(f"lark CLI exited with code {code}. Output: {buffer.getvalue().strip()}")
    finally:
        if old_loader is not None:
            lark_cli.load_and_auth_cookies = old_loader  # type: ignore[assignment]
        sys.argv = old_argv
        os.chdir(old_cwd)
    return buffer.getvalue().strip()


def _execute_doc_read(skill_root: Path, request_text: str) -> str:
    from lark_tools.commands.doc import cmd_doc_read

    url_match = URL_RE.search(request_text or "")
    if not url_match:
        raise RuntimeError("No Feishu document URL found in the request.")
    target = url_match.group(0)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cmd_doc_read(_load_cookies(skill_root), target)
    return buffer.getvalue().strip()


def _execute_whiteboard_read(skill_root: Path, request_text: str) -> str:
    from lark_tools.commands.whiteboard import cmd_whiteboard_read

    token_match = BLOCK_TOKEN_RE.search(request_text or "")
    target = token_match.group(1) if token_match else ""
    if not target:
        url_match = URL_RE.search(request_text or "")
        target = url_match.group(0) if url_match else ""
    if not target:
        raise RuntimeError("No Feishu whiteboard URL or blockToken found in the request.")

    fmt_match = re.search(r"--format\s+([A-Za-z0-9_-]+)", request_text)
    fmt = (fmt_match.group(1).strip() if fmt_match else "ai") or "ai"
    if fmt not in {"ai", "json", "raw", "code"}:
        fmt = "ai"

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cmd_whiteboard_read(_load_cookies(skill_root), target, None, fmt, None, 0)
    return buffer.getvalue().strip()


def _extract_bitable_target(request_text: str) -> str:
    url_match = URL_RE.search(request_text or "")
    if url_match:
        return url_match.group(0)

    explicit_token = re.search(
        r"(?:base\s*token|bitable\s*token|多维表格\s*token|表格\s*token)\s*[:：]?\s*([A-Za-z0-9]{20,})",
        request_text,
        re.I,
    )
    if explicit_token:
        return explicit_token.group(1)

    generic_token = re.search(r"\b([A-Za-z0-9]{20,})\b", request_text)
    if generic_token:
        return generic_token.group(1)
    raise RuntimeError("No Feishu Base URL or base token found in the request.")


def _execute_bitable_add_table(skill_root: Path, request_text: str) -> str:
    from lark_tools.auth import get_current_user_id, load_user_name
    from lark_tools.commands.bitable_write import cmd_bitable_add_table

    target = _extract_bitable_target(request_text)
    name_match = re.search(r"(?:名为|叫做|名称为|name\s*[:=])\s*([^\s，。；;]+)", request_text, re.I)
    table_name = name_match.group(1).strip() if name_match else "新数据表"

    cookies = _load_cookies(skill_root)
    owner_user_id = get_current_user_id(cookies) or ""
    owner_name = load_user_name() or owner_user_id or "current user"

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cmd_bitable_add_table(
            cookies,
            target,
            table_name,
            user_ticket="",
            owner_user_id=owner_user_id,
            owner_name=owner_name,
            owner_en_name=owner_name,
            local_rev=0,
            member_id=None,
            table_index=1,
        )
    return buffer.getvalue().strip()


def _looks_like_lark_cli_request(text: str) -> bool:
    return bool(LARK_CLI_RE.search(text or ""))


def _looks_like_whiteboard_read_request(text: str) -> bool:
    lowered = (text or "").lower()
    raw = text or ""
    has_whiteboard_target = "blocktoken=" in lowered or "blocktype=whiteboard" in lowered
    mentions_whiteboard = "whiteboard" in lowered or "画板" in raw or "白板" in raw
    wants_read = any(marker in raw for marker in ("读取", "查看", "解析", "总结", "讲解", "内容")) or any(
        marker in lowered for marker in ("read", "parse", "summarize", "summary", "content", "--format ai")
    )
    return (has_whiteboard_target or mentions_whiteboard) and wants_read


def _looks_like_bitable_add_table_request(text: str) -> bool:
    lowered = (text or "").lower()
    raw = text or ""
    if _looks_like_whiteboard_read_request(text):
        return False
    noun_hit = any(marker in raw for marker in ("数据表", "多维表格")) or any(
        marker in lowered for marker in ("base", "bitable", "/base/", "/wiki/")
    )
    verb_hit = any(marker in raw for marker in ("新建", "新增", "创建", "添加")) or any(
        marker in lowered for marker in ("add", "create", "new")
    )
    if not (noun_hit and verb_hit):
        return False
    try:
        _extract_bitable_target(text)
        return True
    except Exception:
        return False


def _looks_like_doc_read_request(text: str) -> bool:
    if _looks_like_whiteboard_read_request(text):
        return False
    lowered = (text or "").lower()
    wants_read = any(marker in lowered for marker in ("read", "parse", "summarize", "summary", "content", "查看", "读取", "解析"))
    return wants_read and any(marker in lowered for marker in ("/docx/", "/wiki/", "feishu", "lark"))


def main() -> int:
    skill_root = Path(os.environ.get("CHAT_AGENT_SKILL_ROOT") or Path(__file__).resolve().parent)
    sys.path.insert(0, str(skill_root))
    _patch_requests_no_proxy()

    payload = _load_payload()
    request_text = str(payload.get("request") or "").strip()

    if _looks_like_lark_cli_request(request_text):
        result = _execute_lark_cli(skill_root, request_text)
    elif _looks_like_bitable_add_table_request(request_text):
        result = _execute_bitable_add_table(skill_root, request_text)
    elif _looks_like_whiteboard_read_request(request_text):
        result = _execute_whiteboard_read(skill_root, request_text)
    elif _looks_like_doc_read_request(request_text):
        result = _execute_doc_read(skill_root, request_text)
    else:
        raise RuntimeError(
            "feishu-personal supports explicit `lark ...` passthrough plus Feishu doc/wiki read, whiteboard read, and Base add-table shortcuts."
        )

    sys.stdout.write(json.dumps({"result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
