"""
lark_tools.cli - Lark/Feishu im/gateway CLI entry point.

Commands: login, search, user, chat, msg, img, doc, bitable, sheet, calendar, test-cmd
All commands output JSON to stdout. Errors/logs go to stderr.

Installed as the `lark` console script via pyproject.toml [project.scripts].
"""
import json
import os
import sys
import time as _time
from pathlib import Path

from lark_tools.intranet import assert_on_intranet
from lark_tools.proto import encode_message
from lark_tools.gateway import send_gateway_request, decode_response
from lark_tools.auth import load_cookies, check_auth, cmd_login
from lark_tools.commands.search import SEARCH_CONFIGS, cmd_search
from lark_tools.commands.chat import (
    get_today_time_range, cmd_chat_today, cmd_chat_messages, cmd_chat_p2p, cmd_chat_thread,
    cmd_chat_members,
)
from lark_tools.commands.user import cmd_user
from lark_tools.commands.msg import cmd_msg
from lark_tools.commands.img import cmd_download_image, fetch_image_crypto
from lark_tools.commands.file import cmd_download_file
from lark_tools.commands.doc import cmd_doc_read, cmd_doc_meta, cmd_doc_length, cmd_doc_download, cmd_doc_blocks
from lark_tools.commands.doc_write import (
    cmd_doc_create, cmd_doc_append, cmd_doc_set_title,
    cmd_doc_delete_block, cmd_doc_edit, cmd_doc_edit_code, cmd_doc_insert_image,
)
from lark_tools.commands.bitable import (
    resolve_bitable_token, resolve_wiki_to_bitable,
    cmd_bitable_tables, cmd_bitable_schema,
    cmd_bitable_records, cmd_bitable_download, cmd_bitable_views,
)
from lark_tools.commands.bitable_write import (
    cmd_bitable_create, cmd_bitable_set_record, cmd_bitable_add_record,
    cmd_bitable_delete_record, cmd_bitable_add_field, cmd_bitable_rename_field,
    cmd_bitable_add_records_batch, cmd_bitable_set_field_format,
)
from lark_tools.commands.calendar import cmd_calendar_list, cmd_calendar_events, cmd_calendar_detail
from lark_tools.commands.minutes import (
    cmd_minutes_meta, cmd_minutes_transcript, cmd_minutes_summary,
    cmd_minutes_chapters, cmd_minutes_download,
    cmd_minutes_list, cmd_minutes_search,
)
from lark_tools.commands.sheet import cmd_sheet_tables, cmd_sheet_read, cmd_sheet_download, cmd_sheet_images
from lark_tools.commands.whiteboard import (
    cmd_whiteboard_list, cmd_whiteboard_meta, cmd_whiteboard_read, cmd_whiteboard_download,
)
from lark_tools.commands.whiteboard_write import (
    cmd_whiteboard_create, cmd_whiteboard_plantuml, cmd_whiteboard_mermaid,
)
from lark_tools.commands.sheet_write import (
    cmd_sheet_create, cmd_sheet_set_cell, cmd_sheet_set_formula, cmd_sheet_set_range,
    cmd_sheet_set_style,
    cmd_sheet_insert_row, cmd_sheet_delete_row, cmd_sheet_insert_col, cmd_sheet_delete_col,
    cmd_sheet_add_tab, cmd_sheet_delete_tab, cmd_sheet_rename_tab, cmd_sheet_state_clear,
)


def _strip_matching_outer_quotes(value: str) -> str:
    text = (value or '').strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _load_batch_records_json(filtered_args: list, start_index: int = 4):
    """Load add-records-batch payload from argv or --json-file.

    JSON payloads can contain spaces, so all remaining argv parts are joined
    before parsing. This keeps explicit CLI usage and model-generated commands
    robust without relying on entity-specific assumptions.
    """
    if '--json-file' in filtered_args:
        file_index = filtered_args.index('--json-file')
        if file_index + 1 >= len(filtered_args):
            raise ValueError('--json-file requires a path')
        path = Path(filtered_args[file_index + 1]).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise ValueError(f'JSON file not found: {path}')
        raw_json = path.read_text(encoding='utf-8-sig')
    else:
        raw_json = ' '.join(filtered_args[start_index:]) if len(filtered_args) > start_index else '[]'
    raw_json = _strip_matching_outer_quotes(raw_json)
    if raw_json.startswith('@'):
        path = Path(raw_json[1:]).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise ValueError(f'JSON file not found: {path}')
        raw_json = path.read_text(encoding='utf-8-sig')
    try:
        records = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(
            'Invalid JSON for records. Pass a JSON array of objects as one argument, '
            'or use --json-file. Example: '
            'lark bitable add-records-batch <url> <tableId> '
            '\'[{"fldXXX":"value"}]\'. '
            f'Parser error: {exc}'
        ) from exc
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise ValueError('records_json must be a JSON array of objects')
    return records


def _parse_delete_record_args(args: list, filtered_args: list):
    """Parse delete-record arguments in positional or option form."""
    table_id = _get_arg(args, '--table') or _get_arg(args, '--table-id')
    record_id = _get_arg(args, '--record-id')
    if not table_id and len(filtered_args) > 3 and not filtered_args[3].startswith('--'):
        table_id = filtered_args[3]
    if not record_id and len(filtered_args) > 4 and not filtered_args[4].startswith('--'):
        record_id = filtered_args[4]
    return table_id, record_id


def _parse_delete_records_args(args: list, filtered_args: list):
    """Parse delete-records arguments into (table_id, record_ids)."""
    table_id = _get_arg(args, '--table') or _get_arg(args, '--table-id')
    raw_ids = _get_arg(args, '--record-ids') or _get_arg(args, '--record-id')
    if not table_id and len(filtered_args) > 3 and not filtered_args[3].startswith('--'):
        table_id = filtered_args[3]
    if raw_ids:
        record_ids = [item.strip() for item in raw_ids.split(',') if item.strip()]
    else:
        record_ids = [
            item.strip()
            for item in filtered_args[4:]
            if item.strip() and not item.startswith('--')
        ]
    return table_id, record_ids
from lark_tools.commands.im_write import cmd_send_text, cmd_send_media, cmd_recall
from lark_tools.audit import audit_write, audit_read
from lark_tools.errors import LarkCliError


_TOP_HELP = """Lark/Feishu CLI — personal-session access via reverse-engineered im/gateway.

Usage:
  lark <command> [args] [--raw]

Commands:
  login                              QR-scan login (auto-triggered on expired session)
  search <query> [--type TYPE]       Search contacts/messages/docs/groups/vc/smart
  chat <sub> ...                     Chat history / P2P / members / today / search
  msg <messageId>                    Single message detail
  img <imageKey> --msg <messageId>   Download + decrypt chat image (AES-256-GCM)
  file <fileKey> --msg <messageId>   Download chat file (msgType=3, no decryption)
  ocr <imagePath>                    Local OCR via macOS Vision (no network)
  doc <sub> <token|url> ...          Read/write cloud docx (read/meta/blocks/create/append/edit/...)
  bitable <sub> <token|url> ...      Bitable read/write (tables/schema/views/records/download/CRUD)
  sheet <sub> <token|url> ...        Spreadsheet tabs/read/download/images
  whiteboard <sub> <bt|url> ...      Whiteboard (画板) list/meta/read/download (PNG)
  calendar <sub> ...                 Calendar events/detail/list
  minutes <sub> [token|url|kw]       Minutes (妙记) meta/transcript/summary/chapters/download/list/search
  user <userId> [...]                User profile (batch supported)
  send <chatId> --text|--image|--file ...   Send message (text/image/file, @mention)
  recall <messageId>                 Recall a sent message
  test-cmd <cmd> [payload_json]      Raw gateway probe (debug)

Global flags:
  --raw             Unparsed protobuf output (debug)
  --version / -V    Print version and exit
  --help / -h       Show this help

Output: JSON to stdout, logs to stderr. Session stored in system keyring (service `lark_cli`).
"""


def _get_arg(args, flag):
    """Return the value after `flag` in args, or None."""
    try:
        idx = args.index(flag)
        return args[idx + 1] if idx + 1 < len(args) else None
    except ValueError:
        return None


def _has_flag(args, flag):
    return flag in args


def _get_repeated_arg(args, flag):
    """Collect every value following an occurrence of `flag` in args."""
    out = []
    i = 0
    while i < len(args):
        if args[i] == flag and i + 1 < len(args):
            out.append(args[i + 1])
            i += 2
        else:
            i += 1
    return out


def _get_repeated_triplet(args, flag, min_arity=2, max_arity=3):
    """Collect tuples of (min_arity..max_arity) tokens following each `flag`.

    Each occurrence of `flag` consumes every following non-`--`-prefixed
    token until the next flag or end of args, then validates the count is
    within [min_arity, max_arity]. Out-of-range arity raises BITABLE_QUERY_ARG
    rather than silently truncating — otherwise typos like
    `--filter 数量 isGreater 50 99` would drop `99` and quietly mis-filter.
    """
    out = []
    i = 0
    while i < len(args):
        if args[i] == flag:
            tup = []
            j = i + 1
            while j < len(args) and not args[j].startswith('--'):
                tup.append(args[j])
                j += 1
            if not (min_arity <= len(tup) <= max_arity):
                raise LarkCliError(
                    'BITABLE_QUERY_ARG',
                    f"{flag} expects {min_arity}..{max_arity} tokens; got {tup}"
                )
            out.append(tup)
            i = j
        else:
            i += 1
    return out


def _parse_wiki_token_arg(value):
    """Accept a raw wiki_token or a /wiki/<token> URL; return the token.

    /docx/, /sheet/, /base/ URLs use a different token type (obj_token) that
    the wiki tree API does not accept; reject those with a clear error.
    """
    if not value:
        return None
    if '/' not in value:
        return value
    import re
    m = re.search(r'/wiki/([A-Za-z0-9]+)', value)
    if m:
        return m.group(1)
    bad = re.search(r'/(docx|doc|sheet|base|file)/[A-Za-z0-9]+', value)
    if bad:
        raise LarkCliError(
            'PARENT_NOT_WIKI',
            f'--parent must be a wiki node (URL with /wiki/<token>, or raw wiki_token). '
            f'Got /{bad.group(1)}/ URL — that is an obj_token, not a wiki_token. '
            f'Open the doc from the wiki tree (left sidebar 知识库) to get the /wiki/ URL.'
        )
    raise LarkCliError(
        'PARENT_INVALID',
        f'--parent value not recognized: {value!r}. Provide a wiki_token or /wiki/ URL.'
    )


def load_and_auth_cookies():
    """Load cookies, auto-login if session is missing or expired.

    Tries sources in order:
      1. System keyring (lark login)
      2. sessionss/feishu_web_session.json (agent QR login)
    """
    cookies = load_cookies()

    # Fallback: if keyring is empty, try the agent's session file
    if not cookies:
        cookies = _load_cookies_from_sessionss()

    auth_ok = check_auth(cookies) if cookies else False
    if not auth_ok:
        print('[auth] Session expired or missing. Starting QR login...', file=sys.stderr)
        cmd_login()
        cookies = load_cookies()
        if not cookies:
            raise LarkCliError('LOGIN_FAILED', 'Login did not produce cookies.')
        auth_ok = check_auth(cookies)
        if not auth_ok:
            raise LarkCliError('AUTH_FAILED', 'Cookies saved but auth check still fails.')
        print('[auth] Login successful. Continuing with command...', file=sys.stderr)
    return cookies


def _load_cookies_from_sessionss():
    """Try to load the session cookie from the agent's session file."""
    import json as _json
    from pathlib import Path as _Path

    # Look for sessionss/feishu_web_session.json relative to cwd or known paths
    candidates = [
        _Path.cwd() / "sessionss" / "feishu_web_session.json",
        _Path(__file__).resolve().parent.parent.parent / "sessionss" / "feishu_web_session.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = _json.loads(path.read_text(encoding="utf-8"))
            session = str(payload.get("session") or "").strip()
            if session:
                print(f"[auth] Using session from {path}", file=sys.stderr)
                return [{"name": "session", "value": session, "domain": ".feishu.cn"}]
        except Exception:
            continue
    return None


def _dispatch_extended(command, cookies, args, filtered_args, raw):  # noqa: C901
    if command == 'chat':
        sub = filtered_args[1] if len(filtered_args) > 1 else None
        if sub in ('messages', 'msgs'):
            if len(filtered_args) < 3:
                print(json.dumps({'error': 'Missing chatId. Usage: chat messages <chatId> [--count N] [--before POS]'})); sys.exit(1)
            opts = {}
            count_val = _get_arg(args, '--count')
            if count_val: opts['count'] = int(count_val)
            before_val = _get_arg(args, '--before')
            if before_val: opts['before'] = int(before_val)
            if _has_flag(args, '--html'): opts['html'] = True
            if _has_flag(args, '--verbose'): opts['verbose'] = True
            if _has_flag(args, '--compact'): opts['compact'] = True
            with audit_read('chat.messages', target=filtered_args[2], extra=opts or None):
                cmd_chat_messages(cookies, filtered_args[2], raw, opts)
        elif sub == 'p2p':
            if len(filtered_args) < 3:
                print(json.dumps({'error': 'Missing userId. Usage: chat p2p <userId>'})); sys.exit(1)
            with audit_read('chat.p2p', target=filtered_args[2]):
                cmd_chat_p2p(cookies, filtered_args[2], raw)
        elif sub == 'thread':
            # chat thread <chatId> <threadId> [--count N] [--compact|--verbose|--html]
            if len(filtered_args) < 4:
                print(json.dumps({'error': 'Missing args. Usage: chat thread <chatId> <threadId> [--count N]'})); sys.exit(1)
            t_chat_id = filtered_args[2]
            t_thread_id = filtered_args[3]
            t_opts = {}
            tc = _get_arg(args, '--count')
            if tc: t_opts['count'] = int(tc)
            if _has_flag(args, '--html'): t_opts['html'] = True
            if _has_flag(args, '--verbose'): t_opts['verbose'] = True
            if _has_flag(args, '--compact'): t_opts['compact'] = True
            with audit_read('chat.thread', target=f'{t_chat_id}:{t_thread_id}', extra=t_opts or None):
                cmd_chat_thread(cookies, t_chat_id, t_thread_id, raw, t_opts)
        elif sub == 'members':
            if len(filtered_args) < 3:
                print(json.dumps({'error': 'Missing chatId. Usage: chat members <chatId> [--max N] [--page-size N]'})); sys.exit(1)
            mem_opts = {}
            max_val = _get_arg(args, '--max')
            if max_val: mem_opts['max'] = int(max_val)
            ps_val = _get_arg(args, '--page-size')
            if ps_val: mem_opts['pageSize'] = int(ps_val)
            with audit_read('chat.members', target=filtered_args[2], extra=mem_opts or None):
                cmd_chat_members(cookies, filtered_args[2], raw, mem_opts)
        elif sub == 'today':
            chat_opts = {'noBot': not _has_flag(args, '--with-bot'), 'compact': True}
            mt_val = _get_arg(args, '--member-threshold')
            if mt_val: chat_opts['memberThreshold'] = int(mt_val)
            if _has_flag(args, '--html'): chat_opts['html'] = True; chat_opts['compact'] = False
            if _has_flag(args, '--verbose'): chat_opts['verbose'] = True; chat_opts['compact'] = False
            if _has_flag(args, '--all'): chat_opts['all'] = True
            with audit_read('chat.today', extra=chat_opts or None):
                cmd_chat_today(cookies, chat_opts)
        elif sub == 'search':
            chat_opts = {'noBot': not _has_flag(args, '--with-bot'), 'compact': True}
            if _has_flag(args, '--week'): chat_opts['week'] = True
            cf_from = _get_arg(args, '--from')
            cf_to = _get_arg(args, '--to')
            if cf_from: chat_opts['from'] = int(cf_from)
            if cf_to: chat_opts['to'] = int(cf_to)
            if chat_opts.get('from') and not chat_opts.get('to'):
                chat_opts['to'] = int(_time.time())
            cl_limit = _get_arg(args, '--limit')
            if cl_limit: chat_opts['limit'] = int(cl_limit)
            mt_val2 = _get_arg(args, '--member-threshold')
            if mt_val2: chat_opts['memberThreshold'] = int(mt_val2)
            if _has_flag(args, '--html'): chat_opts['html'] = True; chat_opts['compact'] = False
            if _has_flag(args, '--verbose'): chat_opts['verbose'] = True; chat_opts['compact'] = False
            if _has_flag(args, '--all'): chat_opts['all'] = True
            with audit_read('chat.search', extra=chat_opts or None):
                cmd_chat_today(cookies, chat_opts)
        else:
            print(json.dumps({'error': 'Usage: chat messages <chatId> | chat p2p <userId> | chat thread <chatId> <threadId> | chat members <chatId> | chat today | chat search [--week|--from N --to N] [--limit N] [--with-bot] [--compact|--verbose|--html]'}))
            sys.exit(1)

    elif command == 'msg':
        msg_id = filtered_args[1] if len(filtered_args) > 1 else None
        if not msg_id:
            print(json.dumps({'error': 'Missing messageId. Usage: msg <messageId>'})); sys.exit(1)
        with audit_read('msg', target=msg_id):
            cmd_msg(cookies, msg_id, raw)

    elif command == 'img':
        image_key = filtered_args[1] if len(filtered_args) > 1 else None
        if not image_key:
            print(json.dumps({'error': 'Missing imageKey. Usage: img <imageKey> --msg <messageId> [--out PATH|outputPath]'}))
            sys.exit(1)
        msg_id = output_path = None
        i = 2
        while i < len(filtered_args):
            if filtered_args[i] == '--msg' and i + 1 < len(filtered_args):
                i += 1; msg_id = filtered_args[i]
            elif filtered_args[i] == '--out' and i + 1 < len(filtered_args):
                i += 1; output_path = filtered_args[i]
            elif not output_path:
                output_path = filtered_args[i]
            i += 1
        crypto_info = None
        if msg_id:
            print(f'[img] Fetching crypto info from message {msg_id}...', file=sys.stderr)
            crypto_info = fetch_image_crypto(cookies, msg_id, image_key)
        if crypto_info:
            print(f"[img] Found crypto: type={crypto_info.get('cipherType')}, key={len(crypto_info.get('secretKey', b''))}B", file=sys.stderr)
        elif msg_id:
            print('[img] Warning: could not extract crypto info, image may be encrypted', file=sys.stderr)
        with audit_read('img', target=image_key):
            cmd_download_image(cookies, image_key, output_path, crypto_info)

    elif command == 'file':
        # file <fileKey> --msg <messageId> [--out PATH|outputPath]
        file_key = filtered_args[1] if len(filtered_args) > 1 else None
        if not file_key:
            print(json.dumps({'error': 'Missing fileKey. Usage: file <fileKey> --msg <messageId> [--out PATH|outputPath]'}))
            sys.exit(1)
        f_msg_id = f_output_path = None
        i = 2
        while i < len(filtered_args):
            if filtered_args[i] == '--msg' and i + 1 < len(filtered_args):
                i += 1; f_msg_id = filtered_args[i]
            elif filtered_args[i] == '--out' and i + 1 < len(filtered_args):
                i += 1; f_output_path = filtered_args[i]
            elif not f_output_path:
                f_output_path = filtered_args[i]
            i += 1
        if not f_msg_id:
            print(json.dumps({'error': 'Missing --msg. Usage: file <fileKey> --msg <messageId> [outputPath]. Run `lark chat messages <chatId>` to find the messageId.'}))
            sys.exit(1)
        with audit_read('file', target=file_key):
            cmd_download_file(cookies, file_key, f_msg_id, f_output_path)

    elif command == 'doc':
        sub = filtered_args[1] if len(filtered_args) > 1 else None
        token = filtered_args[2] if len(filtered_args) > 2 else None
        if sub == 'read':
            if not token: print(json.dumps({'error': 'Missing token. Usage: doc read <token|url>'})); sys.exit(1)
            with audit_read('doc.read', target=token):
                cmd_doc_read(cookies, token)
        elif sub == 'meta':
            if not token: print(json.dumps({'error': 'Missing token. Usage: doc meta <token|url>'})); sys.exit(1)
            with audit_read('doc.meta', target=token):
                cmd_doc_meta(cookies, token)
        elif sub == 'length':
            if not token: print(json.dumps({'error': 'Missing token. Usage: doc length <token|url>'})); sys.exit(1)
            with audit_read('doc.length', target=token):
                cmd_doc_length(cookies, token)
        elif sub == 'download':
            dl_token = filtered_args[2] if len(filtered_args) > 2 else None
            if not dl_token: print(json.dumps({'error': 'Missing token. Usage: doc download <token|url> [--out PATH|output.md]'})); sys.exit(1)
            # --out wins; positional is fallback for back-compat
            out_path = _get_arg(args, '--out')
            if not out_path and len(filtered_args) > 3 and not filtered_args[3].startswith('--'):
                out_path = filtered_args[3]
            with audit_read('doc.download', target=dl_token):
                cmd_doc_download(cookies, dl_token, out_path, True)
        elif sub == 'create':
            # doc create <title> [--text "..." | --md-file path | --stdin] [--parent <wiki_token>]
            title = filtered_args[2] if len(filtered_args) > 2 else ''
            text_val = _get_arg(filtered_args, '--text')
            md_file = _get_arg(filtered_args, '--md-file')
            from_stdin = _has_flag(filtered_args, '--stdin')
            parent_token = _parse_wiki_token_arg(_get_arg(filtered_args, '--parent'))
            extra = {'title': title} if title else {}
            if parent_token:
                extra['parent'] = parent_token
            with audit_write('doc.create', extra=extra or None):
                cmd_doc_create(cookies, title, text_val, md_file, from_stdin,
                               parent_wiki_token=parent_token)
        elif sub == 'append':
            if not token: print(json.dumps({'error': 'Missing token. Usage: doc append <token|url> [--text "..." | --md-file path | --stdin]'})); sys.exit(1)
            text_val = _get_arg(filtered_args, '--text')
            md_file = _get_arg(filtered_args, '--md-file')
            from_stdin = _has_flag(filtered_args, '--stdin')
            with audit_write('doc.append', target=token):
                cmd_doc_append(cookies, token, text_val, md_file, from_stdin)
        elif sub == 'set-title':
            new_title = filtered_args[3] if len(filtered_args) > 3 else None
            if not token or not new_title: print(json.dumps({'error': 'Usage: doc set-title <token|url> <new_title>'})); sys.exit(1)
            with audit_write('doc.set-title', target=token):
                cmd_doc_set_title(cookies, token, new_title)
        elif sub == 'delete-block':
            block_id = filtered_args[3] if len(filtered_args) > 3 else None
            if not token or not block_id: print(json.dumps({'error': 'Usage: doc delete-block <token|url> <block_id>'})); sys.exit(1)
            with audit_write('doc.delete-block', target=token, extra={'block_id': block_id}):
                cmd_doc_delete_block(cookies, token, block_id)
        elif sub == 'edit':
            block_id = filtered_args[3] if len(filtered_args) > 3 else None
            replace_text = _get_arg(filtered_args, '--replace')
            if not token or not block_id or replace_text is None:
                print(json.dumps({'error': 'Usage: doc edit <token|url> <block_id> --replace "<new text>"'})); sys.exit(1)
            with audit_write('doc.edit', target=token, extra={'block_id': block_id}):
                cmd_doc_edit(cookies, token, block_id, replace_text)
        elif sub == 'insert-image':
            image_path = filtered_args[3] if len(filtered_args) > 3 else None
            if not token or not image_path:
                print(json.dumps({'error': 'Usage: doc insert-image <token|url> <image_path>'})); sys.exit(1)
            with audit_write('doc.insert-image', target=token):
                cmd_doc_insert_image(cookies, token, image_path)
        elif sub == 'blocks':
            if not token:
                print(json.dumps({'error': 'Usage: doc blocks <token|url> [--type T] [--grep PATTERN] [--limit N] [--offset N] [--format json|compact] [--preview N]'})); sys.exit(1)
            type_filter = _get_arg(args, '--type')
            preview_str = _get_arg(args, '--preview')
            preview = int(preview_str) if preview_str else 60
            grep = _get_arg(args, '--grep')
            limit_str = _get_arg(args, '--limit')
            offset_str = _get_arg(args, '--offset')
            fmt = _get_arg(args, '--format') or 'json'
            with audit_read('doc.blocks', target=token):
                cmd_doc_blocks(
                    cookies, token, type_filter, preview,
                    grep=grep,
                    limit=int(limit_str) if limit_str else None,
                    offset=int(offset_str) if offset_str else 0,
                    fmt=fmt,
                )
        elif sub == 'edit-code':
            block_id = filtered_args[3] if len(filtered_args) > 3 else None
            if not token or not block_id:
                print(json.dumps({'error': 'Usage: doc edit-code <token|url> <block_id> [--language LANG] [--content TEXT | --content-file PATH]'})); sys.exit(1)
            new_lang = _get_arg(args, '--language')
            new_content = _get_arg(args, '--content')
            content_file = _get_arg(args, '--content-file')
            with audit_write('doc.edit-code', target=token, extra={'block_id': block_id}):
                cmd_doc_edit_code(cookies, token, block_id, new_lang, new_content, content_file)
        else:
            print(json.dumps({'error': 'Usage: doc read|meta|length|download|blocks|create|append|set-title|delete-block|edit|edit-code|insert-image <token|url>'})); sys.exit(1)

    elif command == 'bitable':
        sub = filtered_args[1] if len(filtered_args) > 1 else None
        if sub == 'create':
            # bitable create [title] [--tz Asia/Shanghai] [--parent <wiki_token>]
            title = filtered_args[2] if len(filtered_args) > 2 and not filtered_args[2].startswith('--') else ''
            tz = _get_arg(args, '--tz')
            parent_token = _parse_wiki_token_arg(_get_arg(args, '--parent'))
            extra = {'title': title} if title else {}
            if parent_token:
                extra['parent'] = parent_token
            with audit_write('bitable.create', extra=extra or None):
                cmd_bitable_create(cookies, title, tz, parent_wiki_token=parent_token)
            return
        token_arg = filtered_args[2] if len(filtered_args) > 2 else None
        if not sub or not token_arg:
            print(json.dumps({'error': 'Usage: bitable create [title] | tables|schema|views|download <token|url> [tableId] | records <token|url> [tableId] [--limit N] [--offset N] [--view ID|NAME] [--filter <field> <op> [value]]... [--sort field[:asc|desc]]... [--group field] [--all]  (op: is|isNot|isGreater|isGreaterEqual|isLess|isLessEqual|contains|doesNotContain|isEmpty|isNotEmpty)'}))
            sys.exit(1)
        parsed = resolve_bitable_token(token_arg)
        bt_token = parsed['token']
        if parsed['is_wiki']:
            bt_token = resolve_wiki_to_bitable(cookies, bt_token)
        url_table_id = parsed['table_id']
        url_view_id = parsed.get('view_id')
        if sub == 'tables':
            with audit_read('bitable.tables', target=bt_token):
                cmd_bitable_tables(cookies, bt_token)
        elif sub == 'schema':
            table_id = filtered_args[3] if len(filtered_args) > 3 and not filtered_args[3].startswith('--') else url_table_id
            with audit_read('bitable.schema', target=bt_token, extra={'table_id': table_id} if table_id else None):
                cmd_bitable_schema(cookies, bt_token, table_id)
        elif sub == 'records':
            table_id = filtered_args[3] if len(filtered_args) > 3 and not filtered_args[3].startswith('--') else url_table_id
            opts = {}
            lim_val = _get_arg(args, '--limit')
            if lim_val: opts['limit'] = int(lim_val)
            off_val = _get_arg(args, '--offset')
            if off_val: opts['offset'] = int(off_val)
            # Explicit --view wins over URL's ?view=; URL is the fallback so
            # pasting a Feishu link that already pins a view honours it.
            view_val = _get_arg(args, '--view') or url_view_id
            if view_val: opts['view'] = view_val
            grp_val = _get_arg(args, '--group')
            if grp_val: opts['group'] = grp_val
            filters = _get_repeated_triplet(args, '--filter')
            if filters: opts['filters'] = filters
            sorts = _get_repeated_arg(args, '--sort')
            if sorts: opts['sorts'] = sorts
            if _has_flag(args, '--all'): opts['all'] = True
            with audit_read('bitable.records', target=bt_token, extra={'table_id': table_id, **opts} if table_id else opts or None):
                try:
                    cmd_bitable_records(cookies, bt_token, table_id, opts)
                except ValueError as e:
                    raise LarkCliError('BITABLE_QUERY_ARG', str(e))
        elif sub == 'views':
            table_id = filtered_args[3] if len(filtered_args) > 3 and not filtered_args[3].startswith('--') else url_table_id
            with audit_read('bitable.views', target=bt_token, extra={'table_id': table_id} if table_id else None):
                cmd_bitable_views(cookies, bt_token, table_id)
        elif sub == 'download':
            table_id = filtered_args[3] if len(filtered_args) > 3 and not filtered_args[3].startswith('--') else url_table_id
            out_path = _get_arg(args, '--out')
            if not out_path and len(filtered_args) > 4 and not filtered_args[4].startswith('--'):
                out_path = filtered_args[4]
            with audit_read('bitable.download', target=bt_token, extra={'table_id': table_id} if table_id else None):
                cmd_bitable_download(cookies, bt_token, table_id, out_path)
        elif sub == 'set-record':
            # bitable set-record <token|url> <tableId> <recordId> <field=value>...
            if len(filtered_args) < 6:
                print(json.dumps({'error': 'Usage: bitable set-record <token|url> <tableId> <recordId> <field=value>...'})); sys.exit(1)
            table_id = filtered_args[3]
            record_id = filtered_args[4]
            kv_pairs = filtered_args[5:]
            with audit_write('bitable.set-record', target=bt_token, extra={'table_id': table_id, 'record_id': record_id}):
                cmd_bitable_set_record(cookies, token_arg, table_id, record_id, kv_pairs)
        elif sub == 'add-record':
            # bitable add-record <token|url> <tableId> <field=value>...
            if len(filtered_args) < 5:
                print(json.dumps({'error': 'Usage: bitable add-record <token|url> <tableId> <field=value>...'})); sys.exit(1)
            table_id = filtered_args[3]
            kv_pairs = filtered_args[4:]
            with audit_write('bitable.add-record', target=bt_token, extra={'table_id': table_id}):
                cmd_bitable_add_record(cookies, token_arg, table_id, kv_pairs)
        elif sub == 'add-records-batch':
            # bitable add-records-batch <token|url> <tableId> <records_json>
            # bitable add-records-batch <token|url> <tableId> --json-file <path>
            # bitable add-records-batch <token|url> <tableId> @path
            # records_json: [{"fldXXX":"val1","fldYYY":"val2"}, ...]
            if len(filtered_args) < 4:
                print(json.dumps({'error': 'Usage: bitable add-records-batch <token|url> <tableId> <records_json|--json-file path>'})); sys.exit(1)
            table_id = filtered_args[3]
            try:
                records = _load_batch_records_json(filtered_args, 4)
            except ValueError as exc:
                print(json.dumps({'error': str(exc)}, ensure_ascii=False)); sys.exit(1)
            with audit_write('bitable.add-records-batch', target=bt_token, extra={'table_id': table_id, 'count': len(records)}):
                cmd_bitable_add_records_batch(cookies, token_arg, table_id, records)
        elif sub == 'delete-record':
            # bitable delete-record <token|url> <tableId> <recordId>
            # bitable delete-record <token|url> --table <tableId> --record-id <recordId>
            table_id, record_id = _parse_delete_record_args(args, filtered_args)
            if not table_id or not record_id:
                print(json.dumps({'error': 'Usage: bitable delete-record <token|url> <tableId> <recordId> OR bitable delete-record <token|url> --table <tableId> --record-id <recordId>'})); sys.exit(1)
            with audit_write('bitable.delete-record', target=bt_token, extra={'table_id': table_id, 'record_id': record_id}):
                cmd_bitable_delete_record(cookies, token_arg, table_id, record_id)
        elif sub == 'delete-records':
            # bitable delete-records <token|url> <tableId> <recordId>...
            # bitable delete-records <token|url> --table <tableId> --record-ids <id1,id2>
            table_id, record_ids = _parse_delete_records_args(args, filtered_args)
            if not table_id or not record_ids:
                print(json.dumps({'error': 'Usage: bitable delete-records <token|url> <tableId> <recordId>... OR bitable delete-records <token|url> --table <tableId> --record-ids <id1,id2>'})); sys.exit(1)
            deleted = []
            with audit_write('bitable.delete-records', target=bt_token, extra={'table_id': table_id, 'count': len(record_ids)}):
                for record_id in record_ids:
                    cmd_bitable_delete_record(cookies, token_arg, table_id, record_id)
                    deleted.append(record_id)
            print(json.dumps({'success': True, 'table_id': table_id, 'records_deleted': len(deleted), 'record_ids': deleted}, ensure_ascii=False, indent=2))
        elif sub == 'add-field':
            # bitable add-field <token|url> <tableId> <name> [--type text|number|...] [--format 0.###]
            if len(filtered_args) < 5:
                print(json.dumps({'error': 'Usage: bitable add-field <token|url> <tableId> <name> [--type text|number|checkbox|url|datetime|singleselect|multiselect] [--format 0.###]'})); sys.exit(1)
            table_id = filtered_args[3]
            name = filtered_args[4]
            ftype = _get_arg(args, '--type') or 'text'
            number_format = _get_arg(args, '--format')
            with audit_write('bitable.add-field', target=bt_token, extra={'table_id': table_id, 'name': name, 'type': ftype, 'format': number_format}):
                cmd_bitable_add_field(cookies, token_arg, table_id, name, ftype, number_format)
        elif sub == 'set-field-format':
            # bitable set-field-format <token|url> <tableId> <field_id_or_name> <format>
            if len(filtered_args) < 6:
                print(json.dumps({'error': 'Usage: bitable set-field-format <token|url> <tableId> <field_id_or_name> <format>'})); sys.exit(1)
            table_id = filtered_args[3]
            fid_or_name = filtered_args[4]
            number_format = filtered_args[5]
            with audit_write('bitable.set-field-format', target=bt_token, extra={'table_id': table_id, 'field': fid_or_name, 'format': number_format}):
                cmd_bitable_set_field_format(cookies, token_arg, table_id, fid_or_name, number_format)
        elif sub == 'rename-field':
            # bitable rename-field <token|url> <tableId> <field_id_or_name> <new_name>
            if len(filtered_args) < 6:
                print(json.dumps({'error': 'Usage: bitable rename-field <token|url> <tableId> <field_id_or_name> <new_name>'})); sys.exit(1)
            table_id = filtered_args[3]
            fid_or_name = filtered_args[4]
            new_name = filtered_args[5]
            with audit_write('bitable.rename-field', target=bt_token, extra={'table_id': table_id}):
                cmd_bitable_rename_field(cookies, token_arg, table_id, fid_or_name, new_name)
        else:
            print(json.dumps({'error': 'Usage: bitable create [title] | tables|schema|views|records|download <token|url> [tableId] | set-record|add-record|delete-record|add-field|set-field-format|rename-field ...'})); sys.exit(1)

    elif command in ('calendar', 'cal'):
        sub = filtered_args[1] if len(filtered_args) > 1 else None
        if sub == 'list':
            with audit_read('calendar.list'):
                cmd_calendar_list(cookies, raw)
        elif sub in ('events', 'ev'):
            ev_opts = {}
            if _has_flag(args, '--today'): ev_opts['today'] = True
            if _has_flag(args, '--month'): ev_opts['month'] = True
            ev_from = _get_arg(args, '--from')
            ev_to = _get_arg(args, '--to')
            if ev_from: ev_opts['from'] = int(ev_from)
            if ev_to: ev_opts['to'] = int(ev_to)
            with audit_read('calendar.events', extra=ev_opts or None):
                cmd_calendar_events(cookies, raw, ev_opts)
        elif sub == 'detail':
            event_id = filtered_args[2] if len(filtered_args) > 2 else None
            if not event_id:
                print(json.dumps({'error': 'Missing eventId. Usage: calendar detail <eventId>'})); sys.exit(1)
            with audit_read('calendar.detail', target=event_id):
                cmd_calendar_detail(cookies, event_id, raw)
        else:
            print(json.dumps({'error': 'Usage: calendar list | calendar events [--today|--month|--from N --to N] | calendar detail <eventId>'}))
            sys.exit(1)

    elif command == 'minutes':
        sub = filtered_args[1] if len(filtered_args) > 1 else None
        if not sub or sub in ('help', '--help', '-h'):
            print('Usage: minutes <meta|transcript|summary|chapters|download|list|search> ...')
            return

        # User-scoped subcommands (no token positional)
        if sub == 'list':
            limit = int(_get_arg(args, '--limit') or 50)
            as_json = _has_flag(args, '--json')
            with audit_read('minutes.list', extra={'limit': limit}):
                cmd_minutes_list(cookies, limit=limit, as_json=as_json)
            return

        if sub == 'search':
            query = filtered_args[2] if len(filtered_args) > 2 else None
            if not query:
                print(json.dumps({'error': 'minutes search: missing <keyword>'})); sys.exit(1)
            limit = int(_get_arg(args, '--limit') or 20)
            as_json = _has_flag(args, '--json')
            with audit_read('minutes.search', target=query, extra={'limit': limit}):
                cmd_minutes_search(cookies, query, limit=limit, as_json=as_json)
            return

        # Token-based subcommands below
        token_arg = filtered_args[2] if len(filtered_args) > 2 else None
        if not token_arg:
            print(json.dumps({'error': f'minutes {sub}: missing <token|url>'})); sys.exit(1)

        if sub == 'meta':
            with audit_read('minutes.meta', target=token_arg):
                cmd_minutes_meta(cookies, token_arg)
        elif sub == 'transcript':
            with audit_read('minutes.transcript', target=token_arg):
                cmd_minutes_transcript(cookies, token_arg)
        elif sub == 'summary':
            with audit_read('minutes.summary', target=token_arg):
                cmd_minutes_summary(cookies, token_arg)
        elif sub == 'chapters':
            with audit_read('minutes.chapters', target=token_arg):
                cmd_minutes_chapters(cookies, token_arg)
        elif sub == 'download':
            out_path = _get_arg(args, '--out')
            with audit_read('minutes.download', target=token_arg, extra={'out': out_path} if out_path else None):
                cmd_minutes_download(cookies, token_arg, out_path)
        else:
            print(json.dumps({'error': f'minutes: unknown subcommand "{sub}". Valid: meta|transcript|summary|chapters|download|list|search'})); sys.exit(1)

    elif command == 'sheet':
        sub = filtered_args[1] if len(filtered_args) > 1 else None
        if not sub:
            print(json.dumps({'error': (
                'Usage: sheet <subcommand> ...\n'
                '  read-side : tables | read | download | images\n'
                '  write-side: create | set-cell | set-formula | set-range | set-style | '
                'insert-row | delete-row | insert-col | delete-col | '
                'add-tab | delete-tab | rename-tab | state-clear'
            )})); sys.exit(1)

        # Subcommands that don't require a token positional
        if sub == 'create':
            opts = {}
            t = _get_arg(args, '--title')
            if t: opts['title'] = t
            p = _get_arg(args, '--parent-wiki')
            if p: opts['parentWiki'] = p
            sp = _get_arg(args, '--space')
            if sp: opts['spaceId'] = sp
            fx = _get_arg(args, '--from-xlsx')
            if fx: opts['fromXlsx'] = fx
            with audit_write('sheet.create', extra=opts or None):
                cmd_sheet_create(cookies, opts)
        elif sub == 'state-clear':
            opts = {}
            tk = _get_arg(args, '--token')
            if tk: opts['token'] = tk
            with audit_write('sheet.state-clear', extra=opts or None):
                cmd_sheet_state_clear(opts)
        else:
            # All other subcommands need <token|url> as filtered_args[2]
            token_arg = filtered_args[2] if len(filtered_args) > 2 else None
            if not token_arg:
                print(json.dumps({'error': f'sheet {sub}: missing <token|url>'})); sys.exit(1)

            if sub == 'tables':
                with audit_read('sheet.tables', target=token_arg):
                    cmd_sheet_tables(cookies, token_arg)
            elif sub in ('read', 'download'):
                sheet_opts = {}
                sheet_name = _get_arg(args, '--sheet')
                if sheet_name: sheet_opts['sheetName'] = sheet_name
                if _has_flag(args, '--all'): sheet_opts['all'] = True
                if sub == 'download':
                    out_arg = _get_arg(args, '--out')
                    if not out_arg and len(filtered_args) > 3 and not filtered_args[3].startswith('--'):
                        out_arg = filtered_args[3]
                    if out_arg: sheet_opts['outputPath'] = out_arg
                    with audit_read('sheet.download', target=token_arg, extra=sheet_opts or None):
                        cmd_sheet_download(cookies, token_arg, sheet_opts)
                else:
                    with audit_read('sheet.read', target=token_arg, extra=sheet_opts or None):
                        cmd_sheet_read(cookies, token_arg, sheet_opts)
            elif sub == 'images':
                # sheet images <url> [--sheet NAME] [--cell A1] [--download] [--out PATH]
                sheet_opts = {}
                sheet_name = _get_arg(args, '--sheet')
                if sheet_name: sheet_opts['sheetName'] = sheet_name
                cell = _get_arg(args, '--cell')
                if cell: sheet_opts['cell'] = cell
                if _has_flag(args, '--download'): sheet_opts['download'] = True
                out = _get_arg(args, '--out')
                if out: sheet_opts['outputPath'] = out
                with audit_read('sheet.images', target=token_arg, extra=sheet_opts or None):
                    cmd_sheet_images(cookies, token_arg, sheet_opts)
            elif sub == 'set-cell':
                # sheet set-cell <url> <range> <value> [--sheet NAME] [--style JSON]
                if len(filtered_args) < 5:
                    print(json.dumps({'error': 'Usage: sheet set-cell <url> <range> <value> [--sheet NAME] [--style JSON]'})); sys.exit(1)
                rng = filtered_args[3]
                value = filtered_args[4]
                opts = {}
                sn = _get_arg(args, '--sheet')
                if sn: opts['sheetName'] = sn
                style = _get_arg(args, '--style')
                if style: opts['style'] = style
                if _has_flag(args, '--raw-string'): opts['raw_string'] = True
                with audit_write('sheet.set-cell', target=token_arg, extra={'range': rng, **opts}):
                    cmd_sheet_set_cell(cookies, token_arg, rng, value, opts)
            elif sub == 'set-formula':
                # sheet set-formula <url> <cell> <formula> [--sheet NAME]
                if len(filtered_args) < 5:
                    print(json.dumps({'error': 'Usage: sheet set-formula <url> <cell> <formula> [--sheet NAME]'})); sys.exit(1)
                cell = filtered_args[3]
                formula = filtered_args[4]
                opts = {}
                sn = _get_arg(args, '--sheet')
                if sn: opts['sheetName'] = sn
                with audit_write('sheet.set-formula', target=token_arg, extra={'cell': cell, **opts}):
                    cmd_sheet_set_formula(cookies, token_arg, cell, formula, opts)
            elif sub == 'set-style':
                # sheet set-style <url> <range> --style JSON [--sheet NAME]
                if len(filtered_args) < 4:
                    print(json.dumps({'error': 'Usage: sheet set-style <url> <range> --style JSON [--sheet NAME]'})); sys.exit(1)
                rng = filtered_args[3]
                style = _get_arg(args, '--style')
                if not style:
                    print(json.dumps({'error': 'sheet set-style: --style JSON is required'})); sys.exit(1)
                opts = {'style': style}
                sn = _get_arg(args, '--sheet')
                if sn: opts['sheetName'] = sn
                with audit_write('sheet.set-style', target=token_arg, extra={'range': rng}):
                    cmd_sheet_set_style(cookies, token_arg, rng, opts)
            elif sub == 'set-range':
                # sheet set-range <url> <start_cell> <values_json> [--style JSON] [--sheet NAME]
                if len(filtered_args) < 5:
                    print(json.dumps({'error': 'Usage: sheet set-range <url> <start_cell> <values_json> [--style JSON] [--sheet NAME]'})); sys.exit(1)
                start_cell = filtered_args[3]
                values_json = filtered_args[4]
                opts = {}
                sn = _get_arg(args, '--sheet')
                if sn: opts['sheetName'] = sn
                style = _get_arg(args, '--style')
                if style: opts['style'] = style
                if _has_flag(args, '--raw-string'): opts['raw_string'] = True
                with audit_write('sheet.set-range', target=token_arg, extra={'startCell': start_cell, **opts}):
                    cmd_sheet_set_range(cookies, token_arg, start_cell, values_json, opts)
            elif sub in ('insert-row', 'delete-row'):
                # sheet (insert|delete)-row <url> --at N [--count N] [--sheet NAME]
                at = _get_arg(args, '--at')
                if not at:
                    print(json.dumps({'error': f'sheet {sub}: --at is required (1-based row number)'})); sys.exit(1)
                opts = {'at': at}
                cnt = _get_arg(args, '--count')
                if cnt: opts['count'] = cnt
                sn = _get_arg(args, '--sheet')
                if sn: opts['sheetName'] = sn
                fn = cmd_sheet_insert_row if sub == 'insert-row' else cmd_sheet_delete_row
                with audit_write(f'sheet.{sub}', target=token_arg, extra=opts):
                    fn(cookies, token_arg, opts)
            elif sub in ('insert-col', 'delete-col'):
                # sheet (insert|delete)-col <url> --at A [--count N] [--sheet NAME]
                at = _get_arg(args, '--at')
                if not at:
                    print(json.dumps({'error': f'sheet {sub}: --at is required (column letter, e.g. A, B, AA)'})); sys.exit(1)
                opts = {'at': at}
                cnt = _get_arg(args, '--count')
                if cnt: opts['count'] = cnt
                sn = _get_arg(args, '--sheet')
                if sn: opts['sheetName'] = sn
                fn = cmd_sheet_insert_col if sub == 'insert-col' else cmd_sheet_delete_col
                with audit_write(f'sheet.{sub}', target=token_arg, extra=opts):
                    fn(cookies, token_arg, opts)
            elif sub == 'add-tab':
                # sheet add-tab <url> --name NAME [--at N]
                name = _get_arg(args, '--name')
                if not name:
                    print(json.dumps({'error': 'sheet add-tab: --name is required'})); sys.exit(1)
                opts = {'name': name}
                at = _get_arg(args, '--at')
                if at: opts['at'] = at
                with audit_write('sheet.add-tab', target=token_arg, extra=opts):
                    cmd_sheet_add_tab(cookies, token_arg, opts)
            elif sub == 'delete-tab':
                # sheet delete-tab <url> --name NAME
                name = _get_arg(args, '--name')
                if not name:
                    print(json.dumps({'error': 'sheet delete-tab: --name is required'})); sys.exit(1)
                with audit_write('sheet.delete-tab', target=token_arg, extra={'name': name}):
                    cmd_sheet_delete_tab(cookies, token_arg, {'name': name})
            elif sub == 'rename-tab':
                # sheet rename-tab <url> --from OLD --to NEW
                old = _get_arg(args, '--from')
                new = _get_arg(args, '--to')
                if not old or not new:
                    print(json.dumps({'error': 'sheet rename-tab: --from OLD --to NEW are required'})); sys.exit(1)
                opts = {'fromName': old, 'toName': new}
                with audit_write('sheet.rename-tab', target=token_arg, extra={'from': old, 'to': new}):
                    cmd_sheet_rename_tab(cookies, token_arg, opts)
            else:
                print(json.dumps({'error': f'Unknown sheet subcommand: {sub}'})); sys.exit(1)

    elif command == 'whiteboard':
        sub = filtered_args[1] if len(filtered_args) > 1 else None
        if not sub:
            print(json.dumps({'error': (
                'Usage: whiteboard <subcommand> <token|url> [--block-id <bt>]\n'
                '  read-side :\n'
                '    list     : list whiteboard blocks in a docx (input: docx/wiki URL)\n'
                '    meta     : show whiteboard meta (version, theme, createTime)\n'
                '    read     : dump nodes JSON (--format raw), AI JSON (--format ai),\n'
                '               or extract code (--format code [--index N]) [-o out.{json,puml,mmd}]\n'
                '    download : save server-rendered preview image [-o out.{jpg,png}]\n'
                '  write-side :\n'
                '    create   : create a new empty whiteboard inside a docx (input: docx/wiki URL)\n'
                '    plantuml : render PlantUML and append to whiteboard\n'
                '               <bt|url> [--block-id BT] [--source <file|->] [--text "code"]\n'
                '               [--overwrite] [--dry-run]\n'
                '    mermaid  : render Mermaid (same flags as plantuml)'
            )})); sys.exit(1)

        token_arg = filtered_args[2] if len(filtered_args) > 2 else None
        if not token_arg:
            print(json.dumps({'error': f'whiteboard {sub}: missing <token|url>'})); sys.exit(1)

        block_id_flag = _get_arg(args, '--block-id')

        if sub == 'list':
            with audit_read('whiteboard.list', target=token_arg):
                cmd_whiteboard_list(cookies, token_arg)
        elif sub == 'meta':
            with audit_read('whiteboard.meta', target=token_arg):
                cmd_whiteboard_meta(cookies, token_arg, block_id_flag)
        elif sub == 'read':
            fmt = _get_arg(args, '--format') or 'raw'
            out_path = _get_arg(args, '-o') or _get_arg(args, '--output')
            idx_str = _get_arg(args, '--index')
            idx = int(idx_str) if idx_str else 0
            with audit_read('whiteboard.read', target=token_arg, extra={'format': fmt}):
                cmd_whiteboard_read(cookies, token_arg, block_id_flag, fmt, out_path, idx)
        elif sub == 'download':
            positional_out = filtered_args[3] if len(filtered_args) > 3 and not filtered_args[3].startswith('--') else None
            # --out is the canonical flag; -o/--output kept as aliases for back-compat
            out_path = _get_arg(args, '--out') or _get_arg(args, '-o') or _get_arg(args, '--output') or positional_out
            with audit_read('whiteboard.download', target=token_arg):
                cmd_whiteboard_download(cookies, token_arg, block_id_flag, out_path)
        elif sub == 'create':
            # whiteboard create <docx-url>
            with audit_write('whiteboard.create', target=token_arg):
                cmd_whiteboard_create(cookies, token_arg)
        elif sub in ('plantuml', 'mermaid'):
            # whiteboard <plantuml|mermaid> <bt|url> [positional_file] [--source <file|->] [--text TEXT] [--overwrite] [--dry-run]
            positional_src = filtered_args[3] if len(filtered_args) > 3 and not filtered_args[3].startswith('--') else None
            source_flag = _get_arg(args, '--source')
            text_arg = _get_arg(args, '--text')
            source_path = source_flag or positional_src
            overwrite = _has_flag(args, '--overwrite')
            dry_run = _has_flag(args, '--dry-run')
            fn = cmd_whiteboard_plantuml if sub == 'plantuml' else cmd_whiteboard_mermaid
            extra = {'overwrite': overwrite, 'dry_run': dry_run, 'source': source_path or '(text)'}
            with audit_write(f'whiteboard.{sub}', target=token_arg, extra=extra):
                fn(cookies, token_arg, block_id_flag, source_path, text_arg, overwrite, dry_run)
        else:
            print(json.dumps({'error': f'Unknown whiteboard subcommand: {sub}'})); sys.exit(1)

    elif command == 'send':
        # send <chatId> --text "<msg>" [--at <userId:displayName> ...] [--at all]
        # send <chatId> --image <path>
        # send <chatId> --file <path>
        if len(filtered_args) < 2:
            print(json.dumps({'error': 'Usage: send <chatId> (--text "<msg>" [--at <userId>:<displayName>|all]...) | --image <path> | --file <path>'}))
            sys.exit(1)
        chat_id = filtered_args[1]
        img_path = _get_arg(args, '--image')
        file_path = _get_arg(args, '--file')
        if img_path or file_path:
            local_path = img_path or file_path
            with audit_write('send.media', target=chat_id, extra={'media': os.path.basename(local_path)}):
                cmd_send_media(cookies, chat_id, local_path, raw)
            return
        text = _get_arg(args, '--text')
        if text is None:
            print(json.dumps({'error': 'Missing --text / --image / --file. Usage: send <chatId> --text "<msg>" | --image <path> | --file <path>'}))
            sys.exit(1)
        # Collect all --at occurrences
        at_segments = []
        i = 0
        while i < len(args):
            if args[i] == '--at' and i + 1 < len(args):
                spec = args[i + 1]
                if spec == 'all':
                    at_segments.append(('all', '@所有人'))
                elif ':' in spec:
                    uid, name = spec.split(':', 1)
                    display = name if name.startswith('@') else f'@{name}'
                    at_segments.append((uid, display))
                else:
                    print(json.dumps({'error': f'Invalid --at value: {spec!r}. Use "userId:displayName" or "all".'}))
                    sys.exit(1)
                i += 2
            else:
                i += 1
        with audit_write('send.text', target=chat_id, extra={'has_at': bool(at_segments)} if at_segments else None):
            cmd_send_text(cookies, chat_id, text, at_segments or None, raw)

    elif command == 'recall':
        if len(filtered_args) < 2:
            print(json.dumps({'error': 'Usage: recall <messageId>'}))
            sys.exit(1)
        with audit_write('send.recall', target=filtered_args[1]):
            cmd_recall(cookies, filtered_args[1], raw)

    elif command == 'test-cmd':
        cmd_num_str = filtered_args[1] if len(filtered_args) > 1 else None
        if not cmd_num_str or not cmd_num_str.isdigit():
            print(json.dumps({'error': 'Usage: test-cmd <cmd> [payload_json]'})); sys.exit(1)
        cmd_num = int(cmd_num_str)
        payload_json = filtered_args[2] if len(filtered_args) > 2 else None
        payload = b''
        if payload_json:
            try:
                obj = json.loads(payload_json)
                payload = encode_message(obj)
            except Exception as e:
                print(json.dumps({'error': f'Invalid JSON: {e}'})); sys.exit(1)
        print(f'[test-cmd] Sending cmd={cmd_num}, payload={len(payload)} bytes', file=sys.stderr)
        resp = send_gateway_request(cookies, cmd_num, payload)
        dec = decode_response(resp['buffer'])
        print(json.dumps(dec, indent=2))

    else:
        print(json.dumps({'error': f'Unknown command: {command}. Use: login, search, user, chat, msg, img, file, doc, bitable, sheet, calendar, send, recall, test-cmd'}))
        sys.exit(1)


def _run():
    # Default download dir: ~/Downloads/lark/ (cross-platform)
    dl_dir = os.path.join(os.path.expanduser('~'), 'Downloads', 'lark')
    os.makedirs(dl_dir, exist_ok=True)
    os.chdir(dl_dir)

    args = sys.argv[1:]
    raw = '--raw' in args
    filtered_args = [a for a in args if a != '--raw']
    command = filtered_args[0] if filtered_args else None

    # Top-level version: print __version__ and exit. Handled before help so
    # `lark --version` works even without a subcommand.
    if command in ('--version', '-V'):
        from lark_tools import __version__
        print(__version__)
        return

    # Top-level help: no command, or the first token is a help flag.
    if not command or command in ('help', '--help', '-h'):
        print(_TOP_HELP)
        return

    if command == 'login':
        cmd_login()
        return

    if command == 'ocr':
        img_path = filtered_args[1] if len(filtered_args) > 1 else None
        if not img_path:
            print(json.dumps({'error': 'Missing image path. Usage: ocr <imagePath>'})); sys.exit(1)
        from lark_tools.ocr import cmd_ocr
        cmd_ocr(img_path)
        return

    cookies = load_and_auth_cookies()

    if command == 'search':
        query = filtered_args[1] if len(filtered_args) > 1 else None
        if not query:
            print(json.dumps({'error': 'Missing query. Usage: search <query> [--type contacts|messages|docs|groups|apps|smart] [--today] [--from UNIX] [--to UNIX]'}))
            sys.exit(1)
        s_type = _get_arg(filtered_args, '--type') or 'contacts'
        if s_type not in SEARCH_CONFIGS:
            print(json.dumps({'error': f'Unknown type: {s_type}. Valid: {", ".join(SEARCH_CONFIGS.keys())}'}))
            sys.exit(1)
        search_opts = {}
        if _has_flag(args, '--today'):
            search_opts['timeRange'] = get_today_time_range()
        else:
            s_from = _get_arg(args, '--from')
            s_to = _get_arg(args, '--to')
            if s_from or s_to:
                search_opts['timeRange'] = {
                    'from': int(s_from) if s_from else 0,
                    'to': int(s_to) if s_to else int(_time.time()),
                }
        if _has_flag(args, '--no-bot'):
            search_opts['noBot'] = True
        limit_val = _get_arg(args, '--limit')
        if limit_val:
            search_opts['limit'] = int(limit_val)
        with audit_read('search', target=s_type, extra={'q_len': len(query), **{k: v for k, v in search_opts.items() if k != 'timeRange'}}):
            cmd_search(cookies, query, s_type, raw, search_opts)

    elif command == 'user':
        user_ids = filtered_args[1:]
        if not user_ids:
            print(json.dumps({'error': 'Missing userId. Usage: user <userId> [userId2...]'})); sys.exit(1)
        with audit_read('user', target=user_ids[0] if len(user_ids) == 1 else None, extra={'count': len(user_ids)} if len(user_ids) > 1 else None):
            cmd_user(cookies, user_ids, raw)

    else:
        _dispatch_extended(command, cookies, args, filtered_args, raw)


def main():
    """Entry point for the `lark` console script (and `python -m lark_tools.cli`)."""
    assert_on_intranet()
    try:
        _run()
    except LarkCliError as e:
        _record_and_hint(e)
        print(json.dumps(e.to_json()))
        sys.exit(1)
    except Exception as e:
        _record_and_hint(e)
        print(json.dumps({'error': str(e)}))
        sys.exit(1)


def _record_and_hint(exc: BaseException) -> None:
    """Persist the exception (with traceback) to error.log and print a hint."""
    from lark_tools.audit import log_error, ERROR_LOG_PATH
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    try:
        log_error(cmd, sys.argv[1:], exc)
        print(f'[lark] error logged to {ERROR_LOG_PATH}', file=sys.stderr)
    except Exception:
        # Logging itself must never mask the original error.
        pass


if __name__ == '__main__':
    main()
