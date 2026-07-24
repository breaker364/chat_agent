"""
whiteboard_ot.py - Write protocol for Feishu whiteboard (画板).

Whiteboard writes share the /space/api/whiteboard/user_change endpoint with the
WebSocket OT channel — same payload schema, different transport. We POST it over
HTTP, same as bitable does with /rce/messages.

Three op types observed in capture (all in nodeOperations[]):
  outer type=2 → nodeCreate{path, id, data, index}
  outer type=1 → nodeUpdate{ids, cap, type, paths}
  outer type=3 → nodeDelete{id, path, index}

Syntax type codes (for /whiteboard/parse_syntax `syntax` field):
  0 = raw, 1 = PlantUML, 2 = Mermaid  (mirrors official CLI formatCodeMap)
"""

import json
import random
import string
import time
import uuid

from .config import DOC_HOST
from .http_utils import http_get, http_post_with_cookies


# Op-type constants (outer `type` in each nodeOperations entry)
OP_UPDATE = 1
OP_CREATE = 2
OP_DELETE = 3

# Syntax-type constants (for parse_syntax `syntax` field)
SYNTAX_RAW = 0
SYNTAX_PLANTUML = 1
SYNTAX_MERMAID = 2


# ---------------------------------------------------------------------------
# ID/ticket generators
# ---------------------------------------------------------------------------

def new_member_id() -> int:
    """Client-gen 14-digit member id (fresh per session, server doesn't validate)."""
    return random.randint(10**13, 10**14 - 1)


def new_op_id() -> str:
    """Unique op id. Captured format: a757s-1y2h35433v-c1a20o1u0 (base36-ish)."""
    ts = int(time.time() * 1000)
    a = format(ts % (36**5), 'x')
    b = format(random.getrandbits(48), 'x')
    c = format(random.getrandbits(32), 'x')
    return f'{a}-{b}-{c}'


def new_page_id() -> str:
    """UUID for meta.pageId field."""
    return str(uuid.uuid4())


def new_block_id() -> str:
    """27-char alphanumeric block_id (used as docx-side block id when creating
    a new whiteboard inside a docx).
    """
    alphabet = string.ascii_letters + string.digits
    return ''.join(random.choice(alphabet) for _ in range(27))


def fetch_ws_ticket(cookies) -> str:
    """POST /space/api/pandora_ws/ws_ticket/ {} → ticket string.

    The ticket is per-user-session and accepted by both WS and HTTP user_change
    despite the endpoint name suggesting WS-only.
    """
    res = http_post_with_cookies(cookies, DOC_HOST, '/space/api/pandora_ws/ws_ticket/', {})
    payload = res.get('data') or {}
    if payload.get('code') != 0:
        raise RuntimeError(f'ws_ticket failed: {payload}')
    ticket = (payload.get('data') or {}).get('ticket')
    if not ticket:
        raise RuntimeError(f'ws_ticket: missing ticket in response: {payload}')
    return ticket


def fetch_current_user_id(cookies) -> str:
    """GET /space/api/user/ → user.id (used as meta.userId)."""
    res = http_get(cookies, DOC_HOST, '/space/api/user/')
    payload = res.get('data') or {}
    if payload.get('code') != 0:
        return ''
    data = payload.get('data') or {}
    return data.get('id') or data.get('suid') or ''


# ---------------------------------------------------------------------------
# Op builders
# ---------------------------------------------------------------------------

def op_create(node_data: dict, node_id: str, path: list = None, index: int = 0) -> dict:
    """Build a nodeCreate op."""
    return {
        'type': OP_CREATE,
        'nodeCreate': {
            'path': list(path or []),
            'id': node_id,
            'data': node_data,
            'index': index,
        },
    }


def op_update(path: list, cap: dict, inner_type: int = 0) -> dict:
    """Build a nodeUpdate op. `path` is the chain from root to target node id.
    `cap` is the partial-data dict of changed fields.
    """
    return {
        'type': OP_UPDATE,
        'nodeUpdate': {
            'ids': [],
            'cap': cap,
            'type': inner_type,
            'paths': [{'path': list(path)}],
        },
    }


def op_delete(node_id: str, path: list, index: int = 0) -> dict:
    """Build a nodeDelete op. `path` is the parent chain (without `node_id` at the end)."""
    return {
        'type': OP_DELETE,
        'nodeDelete': {
            'id': node_id,
            'path': list(path),
            'index': index,
        },
    }


# ---------------------------------------------------------------------------
# parse_syntax (DSL → nodes JSON)
# ---------------------------------------------------------------------------

def parse_syntax(cookies, block_token: str, code: str, syntax_type: int) -> list:
    """POST /whiteboard/parse_syntax. Returns the list of node dicts.

    Server response shape: {data: {data: "<stringified JSON of {nodes:[...]}>"}}.
    Does NOT modify the whiteboard — pure parse+render.
    """
    body = {
        'code': code,
        'blockToken': block_token,
        'syntax': syntax_type,
        'reqVersion': 1,
        'parseType': 0,
    }
    res = http_post_with_cookies(cookies, DOC_HOST, '/space/api/whiteboard/parse_syntax', body)
    payload = res.get('data') or {}
    if payload.get('code') != 0:
        raise RuntimeError(f'parse_syntax failed: {payload}')
    inner_str = ((payload.get('data') or {}).get('data')) or '{}'
    try:
        inner = json.loads(inner_str)
    except Exception as e:
        raise RuntimeError(f'parse_syntax: invalid JSON in data.data: {e!r} ({inner_str[:200]!r})')
    return inner.get('nodes') or []


# ---------------------------------------------------------------------------
# user_change envelope
# ---------------------------------------------------------------------------

def build_user_change_envelope(
    block_token: str,
    ops: list,
    member_id: int,
    user_ticket: str,
    user_id: str,
    page_id: str,
    base_seq: int,
    req_id: int = 1,
) -> dict:
    """Construct the full request body for POST /whiteboard/user_change.

    Top-level layout (verified by capture):
      { data: {member_id, user_ticket, uplink: {...}},
        type, entities, version, req_id, context }

    NB: `type/entities/version/req_id/context` are siblings of `data`,
    NOT children of `data`. The server panics if they're nested wrong.
    """
    return {
        'data': {
            'member_id': member_id,
            'user_ticket': user_ticket,
            'uplink': {
                'payload': {
                    'body': {
                        'data': {
                            'nodeOperations': ops,
                            'type': 20,
                            'extData': {'actionTriggerSource': 0},
                        },
                    },
                    'meta': {
                        'deviceId': str(member_id),
                        'userId': user_id,
                        'type': 20,
                        'pageId': page_id,
                    },
                },
                'whiteboardToken': block_token,
                'baseSeq': base_seq,
                'opId': new_op_id(),
                'editTime': int(time.time() * 1000),
                'pkgVersion': 1,
            },
        },
        'type': 'doc',
        'entities': [{
            'isNewProtocol': True,
            'resource_type': 'structure',
            'route_key': block_token,
            'route_type': 'token',
            'token': block_token,
            'type': 'WHITEBOARD',
        }],
        'version': 2,
        'req_id': req_id,
        'context': {
            'os': 'mac',
            'app_version': '2.0.0.9602',
            'os_version': '10.15.7',
            'platform': 'web',
            'request_id': f'lark_tools-{uuid.uuid4().hex[:12]}',
        },
    }


def submit_user_change(cookies, envelope: dict, member_id: int) -> dict:
    """POST the envelope. Returns {seq, editTime, ...} on success; raises otherwise.

    Server response shape: {codeMsg: {code, message}, pkgId, seq, editTime}.

    `Req-Version: 1` header is required — without it the server returns
    `payload is nil [@from@] arg error [@from@] whiteboard` (code 4002000).
    """
    url_path = f'/space/api/whiteboard/user_change?member_id={member_id}'
    res = http_post_with_cookies(
        cookies, DOC_HOST, url_path, envelope,
        extra_headers={'Req-Version': '1'},
    )
    payload = res.get('data') or {}
    code = (payload.get('codeMsg') or {}).get('code')
    if code != 0:
        raise RuntimeError(f'whiteboard user_change failed: {payload}')
    return payload


# ---------------------------------------------------------------------------
# Higher-level: create empty whiteboard in a docx
# ---------------------------------------------------------------------------

def create_whiteboard_data(cookies, docx_token: str, block_id: str) -> str:
    """POST /whiteboard/block/create — server allocates the whiteboard's blockToken.

    `block_id` is the docx-side block id (client-generated). Returns blockToken (server-side).
    """
    body = {
        'baseToken': docx_token,
        'blockId': block_id,
        'baseTokenType': 22,
        'reqVersion': 1,
    }
    res = http_post_with_cookies(cookies, DOC_HOST, '/space/api/whiteboard/block/create', body)
    payload = res.get('data') or {}
    if payload.get('code') != 0:
        raise RuntimeError(f'whiteboard block/create failed: {payload}')
    bt = (payload.get('data') or {}).get('blockToken')
    if not bt:
        raise RuntimeError(f'whiteboard block/create: missing blockToken: {payload}')
    return bt


def insert_whiteboard_into_docx(cookies, docx_token: str, block_id: str,
                                 block_token: str, user_id: str) -> dict:
    """Append a whiteboard block to a docx's children via the docx OT endpoint.

    Mirrors the captured flow:
      1. new block entry: oi with type=whiteboard, token="" then immediately oi+od to set token=block_token
      2. docx root: li block_id at children_count

    Reuses doc_write helpers for the actual POST.
    """
    from .commands.doc_write import (
        fetch_root_block_info, generate_member_id, post_user_change,
    )

    info = fetch_root_block_info(cookies, docx_token)
    member_id = generate_member_id()

    new_block_oi = {
        'parent_id': docx_token,
        'type': 'whiteboard',
        'children': [],
        'comments': [],
        'revisions': [],
        'author': user_id,
        'token': '',
    }

    change_map = {
        block_id: {
            'id': block_id,
            'version': 0,
            'payload': {
                'ops': [
                    {'p': [], 'action': {'oi': new_block_oi}},
                    {'p': ['token'], 'action': {'od': '', 'oi': block_token}},
                ],
            },
        },
        docx_token: {
            'id': docx_token,
            'version': info['version'],
            'payload': {
                'ops': [{'p': ['children', info['children_count']], 'action': {'li': block_id}}],
            },
        },
    }
    return post_user_change(cookies, docx_token, member_id, change_map)


def create_whiteboard(cookies, docx_token: str) -> dict:
    """Full create: whiteboard data + docx block insertion. Returns {block_id, block_token}."""
    block_id = new_block_id()
    user_id = fetch_current_user_id(cookies)
    block_token = create_whiteboard_data(cookies, docx_token, block_id)
    insert_whiteboard_into_docx(cookies, docx_token, block_id, block_token, user_id)
    return {'block_id': block_id, 'block_token': block_token}
