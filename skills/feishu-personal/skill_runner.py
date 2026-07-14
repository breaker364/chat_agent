from __future__ import annotations

import contextlib
import io
import json
import os
import re
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


def _load_cookies(skill_root: Path) -> list[dict[str, str]]:
    return [{"name": "session", "value": _load_session_cookie(skill_root), "domain": ".feishu.cn"}]


def _validate_cookies(skill_root: Path, cookies: list[dict[str, str]]) -> None:
    """Validate that the session cookie is still accepted by the Feishu server.

    Raises RuntimeError with a clear diagnostic if the session is invalid,
    so the agent gets immediate feedback instead of a cryptic CSRF/403 error
    from a downstream HTTP call.
    """
    try:
        from lark_tools.auth import check_auth
    except ImportError as exc:
        raise RuntimeError(
            f"Cannot import lark_tools.auth to validate session: {exc}"
        ) from exc

    if check_auth(cookies):
        return

    raise RuntimeError(
        "Feishu session is NOT valid on the server. "
        "The stored session cookie has been rejected by the Feishu server "
        "(CSRF token may have been rotated, session expired server-side, or cookie revoked). "
        "DO NOT retry with the same session. "
        "The user must complete a new QR login via feishu_login_status and the QR login flow. "
        "Also call feishu_logout first to remove the stale session file."
    )


def _patch_requests_no_proxy() -> None:
    def request_no_proxy(method: str, url: str, **kwargs):
        # Feishu returns transient CSRF-related cookies on normal API
        # responses. Reusing one Session silently sends those cookies on the
        # next POST without the browser-side CSRF context that created them.
        # A fresh Session preserves explicit authentication cookies while
        # preventing response cookies from leaking into later requests.
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

    argv = _split_lark_command(command_text)

    if not argv:
        raise RuntimeError("Empty lark command.")

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


def _extract_bitable_table_id(request_text: str) -> str:
    match = re.search(r"\b(tbl[A-Za-z0-9]+)\b", request_text or "")
    if match:
        return match.group(1)
    raise RuntimeError("No Bitable table ID found in the request.")


def _extract_json_array(request_text: str) -> list[dict[str, Any]]:
    raw = request_text or ""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\[", raw):
        try:
            value, _ = decoder.raw_decode(raw[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            if not all(isinstance(item, dict) for item in value):
                raise RuntimeError("Batch records JSON must be an array of objects.")
            return value
    raise RuntimeError("No valid JSON array of records found in the request.")


def _execute_bitable_add_records_batch(skill_root: Path, request_text: str) -> str:
    from lark_tools.commands.bitable_write import cmd_bitable_add_records_batch

    target = _extract_bitable_target(request_text)
    table_id = _extract_bitable_table_id(request_text)
    records = _extract_json_array(request_text)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cmd_bitable_add_records_batch(_load_cookies(skill_root), target, table_id, records)
    return buffer.getvalue().strip()


# ---------------------------------------------------------------------------
# Shortcut route detectors — map natural-language requests to CLI commands
# ---------------------------------------------------------------------------

def _detect_bitable_route(request_text: str) -> str | None:
    """Return a ``lark bitable ...`` CLI command string, or None.

    These detectors let the agent use natural language (e.g. "创建一个多维表格")
    instead of remembering the exact CLI syntax.  Each detector returns the
    full CLI command that should be executed.
    """
    raw = request_text or ""
    lowered = raw.lower()
    target = ""
    try:
        target = _extract_bitable_target(raw)
    except RuntimeError:
        pass  # some operations (like create) don't need a target

    # --- Create a new base ---
    if not target and _has_bitable_create_intent(raw):
        name_match = re.search(
            r"(?:名为|叫做|标题[是为]?|title\s*[:=]?\s*)([^\s，。；;]+)", raw, re.I,
        )
        title = name_match.group(1).strip() if name_match else "新多维表格"
        return f"lark bitable create {title}"

    # --- List tables in a base ---
    if target and _has_bitable_list_intent(raw):
        return f"lark bitable tables {target}"

    # --- Read records ---
    if target and _has_bitable_read_intent(raw):
        table_match = re.search(r"(?:table|表)\s*[:=]?\s*([A-Za-z0-9]+)", raw, re.I)
        table_id = table_match.group(1) if table_match else ""
        cmd = f"lark bitable records {target} --all"
        if table_id:
            cmd += f" --table {table_id}"
        return cmd

    # --- Add a single record ---
    if target and _has_bitable_add_record_intent(raw):
        table_match = re.search(r"(?:table|表)\s*[:=]?\s*([A-Za-z0-9]+)", raw, re.I)
        table_id = table_match.group(1) if table_match else ""
        kv_match = re.findall(r"([^\s=]+)=([^\s,，]+)", raw)
        if kv_match:
            pairs = " ".join(f"{k}={v}" for k, v in kv_match)
            if table_id:
                return f"lark bitable add-record {target} {table_id} {pairs}"
            else:
                return f"lark bitable add-record {target} tblTODO {pairs}"
        return None

    # --- Batch add records ---
    if target and _has_bitable_batch_intent(raw):
        json_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if json_match:
            return None
        return None

    # --- Add fields ---
    if target and _has_bitable_add_field_intent(raw):
        table_match = re.search(r"(?:table|表)\s*[:=]?\s*([A-Za-z0-9]+)", raw, re.I)
        table_id = table_match.group(1) if table_match else ""
        name_match = re.findall(r"(?:字段|field|列)\s*[:=]?\s*([^\s,，;；]+)", raw, re.I)
        if name_match:
            fname = name_match[0]
            type_match = re.search(r"(?:type|类型)\s*[:=]?\s*([A-Za-z]+)", raw, re.I)
            ftype = type_match.group(1) if type_match else "text"
            format_match = re.search(r"--format\s+([^\s,，;；]+)", raw, re.I)
            format_arg = f" --format {format_match.group(1)}" if format_match else ""
            if table_id:
                return f"lark bitable add-field {target} {table_id} {fname} --type {ftype}{format_arg}"
            else:
                return f"lark bitable add-field {target} tblTODO {fname} --type {ftype}{format_arg}"
        return None

    return None


# --- Intent detectors ---

def _has_bitable_create_intent(text: str) -> bool:
    raw, low = text or "", (text or "").lower()
    nouns = any(m in raw for m in ("多维表格", "数据表", "bitable", "base"))
    verbs = any(m in raw for m in ("新建", "创建", "创建一个", "create", "new"))
    return nouns and verbs


def _has_bitable_list_intent(text: str) -> bool:
    raw, low = text or "", (text or "").lower()
    nouns = any(m in raw for m in ("表", "表格", "table", "tables"))
    verbs = any(m in raw for m in ("列出", "查看", "list", "show", "有哪些"))
    return nouns and verbs


def _has_bitable_read_intent(text: str) -> bool:
    raw, low = text or "", (text or "").lower()
    nouns = any(m in raw for m in ("记录", "数据", "record", "records"))
    verbs = any(m in raw for m in ("读取", "查看", "读出", "read", "get", "fetch"))
    return nouns and verbs


def _has_bitable_add_record_intent(text: str) -> bool:
    raw, low = text or "", (text or "").lower()
    nouns = any(m in raw for m in ("记录", "数据", "record"))
    verbs = any(m in raw for m in ("添加", "新增", "写入", "add", "write", "insert"))
    single = not any(m in low for m in ("批量", "batch", "多条", "多行", "add-records-batch"))
    return nouns and verbs and single


def _has_bitable_batch_intent(text: str) -> bool:
    raw, low = text or "", (text or "").lower()
    nouns = any(m in raw for m in ("记录", "数据", "record"))
    batch = any(m in raw for m in ("批量", "batch", "多条", "多行"))
    return nouns and batch


def _has_bitable_add_field_intent(text: str) -> bool:
    raw, low = text or "", (text or "").lower()
    nouns = any(m in raw for m in ("字段", "列", "field", "column"))
    verbs = any(m in raw for m in ("添加", "新增", "add", "create", "加"))
    return nouns and verbs


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


def _looks_like_bitable_batch_request(text: str) -> bool:
    raw = text or ""
    lowered = raw.lower()
    if not any(marker in lowered for marker in ("add-records-batch", "batch")) and not any(
        marker in raw for marker in ("批量", "多条", "多行")
    ):
        return False
    try:
        _extract_bitable_target(raw)
        _extract_bitable_table_id(raw)
        _extract_json_array(raw)
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

    # ---- Route 1: explicit lark / lark_cli command (highest priority) ----
    if _looks_like_lark_cli_request(request_text):
        result = _execute_lark_cli(skill_root, request_text)

    # ---- Route 2: direct natural-language bitable batch write ----
    elif _looks_like_bitable_batch_request(request_text):
        result = _execute_bitable_add_records_batch(skill_root, request_text)

    # ---- Route 3: bitable shortcut routes (auto-detect intent) ----
    elif (bitable_cmd := _detect_bitable_route(request_text)) is not None:
        result = _execute_lark_cli(skill_root, bitable_cmd)

    # ---- Route 4: add-table to existing base (deprecated shortcut) ----
    elif _looks_like_bitable_add_table_request(request_text):
        cookies = _load_cookies(skill_root)
        _validate_cookies(skill_root, cookies)
        result = _execute_bitable_add_table(skill_root, request_text)

    # ---- Route 5: whiteboard read ----
    elif _looks_like_whiteboard_read_request(request_text):
        cookies = _load_cookies(skill_root)
        _validate_cookies(skill_root, cookies)
        result = _execute_whiteboard_read(skill_root, request_text)

    # ---- Route 6: doc/wiki read ----
    elif _looks_like_doc_read_request(request_text):
        cookies = _load_cookies(skill_root)
        _validate_cookies(skill_root, cookies)
        result = _execute_doc_read(skill_root, request_text)

    else:
        raise RuntimeError(
            "feishu-personal: could not determine the requested operation.\n"
            "Use the explicit `lark ...` form:\n"
            "  lark bitable create <title>              — create a new base\n"
            "  lark bitable tables <url>                 — list tables\n"
            "  lark bitable records <url> --all          — read all records\n"
            "  lark bitable add-field <url> <tableId> <name> [--type text]\n"
            "  lark bitable add-record <url> <tableId> <field=value>...\n"
            "  lark bitable add-records-batch <url> <tableId> <json_array>\n"
            "  lark doc read <url>                       — read a doc/wiki\n"
            "  lark whiteboard read <url> --format ai    — read a whiteboard"
        )

    sys.stdout.write(json.dumps({"result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
