"""
bitable_ot.py - Bitable OT operation constructors and HTTP submit wrapper.

KEY DISCOVERY (2026-04-17): Although the reference docs state that bitable
edits "must" go through WebSocket (wss://ccm-frontier.feishu.cn/ws/v2),
empirically `POST /space/api/rce/messages` with outer `type: "BITABLE_TABLE"`
accepts the same OT payload and returns `ACCEPT_COMMIT` synchronously.
No WATCH handshake, no ws_ticket, no WS client needed.

Payload structure:
    {
        "type": "BITABLE_TABLE",
        "data": {
            "type": "USER_CHANGES",
            "token": "<tbl_id>",
            "route_key": "<base obj_token>",
            "operations": "<gzip+base64 of OT JSON>",
            "signature": "<uuid>",
            "content_type": "gzip/base64",
            "localRev": 0,
            "lang": "zh",
            "member_id": <int>,
            "user_ticket": "",
        },
        "version": 2,
        "req_id": N,
        "context": { os/os_version/app_version/platform/request_id }
    }
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import random
import string
import uuid
from typing import Optional

from .config import DOC_HOST
from .http_utils import http_post_with_cookies


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------

def _rand_id(prefix: str, length: int = 10) -> str:
    """Generate a random ID like 'recXXXXXXXXXX' (10-char suffix)."""
    alphabet = string.ascii_letters + string.digits
    return prefix + ''.join(random.choice(alphabet) for _ in range(length))


def new_record_id() -> str:
    return _rand_id('rec', 10)


def new_field_id() -> str:
    return _rand_id('fld', 10)


def new_option_id() -> str:
    return _rand_id('opt', 10)


def new_member_id() -> int:
    """Per-session 14-digit client ID used in rce/messages."""
    return random.randint(10**13, 10**14 - 1)


def next_table_rank(last_rank: Optional[str] = None) -> str:
    """Generate a table_rank string.

    Feishu uses a dictionary-ordered string for record ordering. For safe
    append-to-end, we use a timestamp-based value that's lexicographically
    after any 'i000...' style rank the server typically produces.

    Mid-insert between two ranks is NOT supported here — fall back to
    append then move.
    """
    # 'z' is after common 'i...' prefixes; 10 random lowercase chars keeps
    # ties unlikely when appending multiple records rapidly.
    suffix = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    return 'z' + suffix


# ---------------------------------------------------------------------------
# Typed value encoders (per field type)
# ---------------------------------------------------------------------------

def encode_text_value(text: str) -> dict:
    """Text (type:1) → rich-text segment array."""
    return {'type': 1, 'value': [{'type': 'text', 'text': text}]}


def encode_url_value(url: str, display: str = None) -> dict:
    """Url (type:15) → url segment array."""
    return {'type': 15, 'value': [{'type': 'url', 'text': display or url, 'link': url}]}


def encode_email_value(email: str) -> dict:
    """Email (type:1) → url segment with mailto: link."""
    return {'type': 1, 'value': [{'type': 'url', 'text': email, 'link': f'mailto:{email}'}]}


def encode_number_value(n) -> dict:
    """Number / Phone / Currency / Rating / Progress (type:2) → bare number."""
    return {'type': 2, 'value': n}


def encode_checkbox_value(b: bool) -> dict:
    return {'type': 7, 'value': bool(b)}


def encode_single_select_value(opt_id: str) -> dict:
    return {'type': 3, 'value': opt_id}


def encode_multi_select_value(opt_ids: list) -> dict:
    return {'type': 4, 'value': list(opt_ids)}


def encode_datetime_value(unix_ms: int) -> dict:
    return {'type': 5, 'value': int(unix_ms)}


# Single entry-point: pick encoder by raw Python value + field type code.
# Callers that know the field type should use the explicit encoder.
def encode_value_for_field(field_type: int, value) -> dict:
    """Best-effort value encoder given the field's numeric type code.

    field_type → fieldUIType map (per feishu_bitable_ot_protocol.md §6):
        1  = Text / Email / Barcode  → string
        2  = Number / Phone / Currency / Rating / Progress  → number
        3  = SingleSelect  → opt_id string
        4  = MultiSelect   → list of opt_id
        5  = DateTime      → unix ms int
        7  = Checkbox      → bool
       15  = Url           → string
    """
    if value is None:
        return {'type': field_type, 'value': None}
    if field_type == 1:
        return encode_text_value(str(value))
    if field_type == 2:
        return encode_number_value(value)
    if field_type == 3:
        return encode_single_select_value(str(value))
    if field_type == 4:
        if not isinstance(value, list):
            value = [value]
        return encode_multi_select_value([str(v) for v in value])
    if field_type == 5:
        return encode_datetime_value(value)
    if field_type == 7:
        return encode_checkbox_value(value)
    if field_type == 15:
        return encode_url_value(str(value))
    # Fallback: send as text
    return encode_text_value(str(value))


# ---------------------------------------------------------------------------
# OT operation constructors
# ---------------------------------------------------------------------------

def op_set_record(table_id: str, record_id: str, cell_data: dict, view_id: str = '') -> dict:
    """Build a SetRecord operation that updates one or more cells of a record.

    cell_data: {fieldId: typedValue} — each typedValue is a dict like
    {"type": 1, "value": [...]} from one of the encode_*_value helpers.
    """
    return {
        'command': 'SetRecord',
        'actions': [{
            'action': 'data.setRecord',
            'type': 2,
            'tableId': table_id,
            'recordId': record_id,
            'viewId': view_id or '',
            'viewType': 0,
            'data': cell_data,
        }],
        'abnormal': False,
        'source': 1,
    }


def op_add_record(table_id: str, record_id: str, cell_data: dict,
                  table_rank: str = None, view_id: str = '') -> dict:
    """Build an AddRecordsV2 operation appending a new record."""
    return {
        'command': 'AddRecordsV2',
        'actions': [{
            'action': 'data.addRecordV2',
            'type': 2,
            'tableId': table_id,
            'recordId': record_id,
            'viewId': view_id or '',
            'data': {
                'cellData': cell_data or {},
                'tableRank': table_rank or next_table_rank(),
            },
        }],
        'abnormal': False,
        'source': 1,
    }


def op_delete_record(table_id: str, record_id: str) -> dict:
    """Build a DeleteRecordsV2 operation removing a record."""
    return {
        'command': 'DeleteRecordsV2',
        'actions': [{
            'action': 'data.deleteRecordV2',
            'type': 2,
            'tableId': table_id,
            'recordId': record_id,
        }],
        'abnormal': False,
        'source': 1,
    }


def op_add_field(table_id: str, field_id: str, name: str, field_type: int,
                 view_ids: list, total_after: int,
                 ui_type: str = None, property_obj: dict = None,
                 ex_info: dict = None, primary_view_id: str = None) -> dict:
    """Build an AddField operation with the double-action pattern.

    Critical fields per ot_protocol §4.1.2 (verified 2026-04-18):
      - data.addField.data MUST contain `indexes: {viewId: order_int}` and
        `total: <fields after add>`. Without these the server returns
        ACCEPT_COMMIT but the field is silently dropped from fieldMap, and
        the table becomes unreadable (clientvars: FXDB_ERR_PLAN_COLUMN_UNKNOWN).
      - data.addField.data MUST NOT contain `id` (only outer `fieldId`).
      - The AddField operation has `abnormal` but NOT `source`.

    Append-to-end convention: order = total_after for every view.
    """
    if not view_ids:
        raise ValueError('op_add_field requires at least one view_id')
    descriptor_ui = ui_type or _default_ui_type(field_type)
    primary_view_id = primary_view_id or view_ids[0]
    indexes = {vid: total_after for vid in view_ids}

    add_field_data = {
        'name': name,
        'type': field_type,
        'fieldUIType': descriptor_ui,
        'enumerable': False,
        'exInfo': ex_info or {},
        'indexes': indexes,
        'total': total_after,
    }
    descriptor = {
        'id': field_id,
        'name': name,
        'type': field_type,
        'fieldUIType': descriptor_ui,
        'enumerable': False,
        'defaultValueRule': None,
        'property': property_obj or {},
    }
    return {
        'command': 'AddField',
        'actions': [
            {
                'action': 'data.addField',
                'type': 2,
                'tableId': table_id,
                'viewId': primary_view_id,
                'fieldId': field_id,
                'data': add_field_data,
            },
            {
                'action': 'data.setFieldAttr',
                'type': 2,
                'tableId': table_id,
                'viewId': primary_view_id,
                'fieldId': field_id,
                'data': descriptor,
            },
        ],
        'abnormal': False,
    }


def op_rename_field(table_id: str, field_id: str, new_name: str,
                    current_descriptor: dict = None, view_id: str = '') -> dict:
    """Build a SetFieldAttr operation that renames a field.

    The `data` must be a complete FieldDescriptor. If `current_descriptor`
    is provided, we copy it and change the name; otherwise we construct a
    minimal descriptor (works for simple types like Text/Number).
    """
    if current_descriptor:
        descriptor = dict(current_descriptor)
    else:
        descriptor = {
            'id': field_id,
            'type': 1,
            'fieldUIType': 'Text',
            'enumerable': False,
            'defaultValueRule': None,
            'property': {},
        }
    descriptor['name'] = new_name
    descriptor['id'] = field_id
    return {
        'command': 'SetFieldAttr',
        'actions': [{
            'action': 'data.setFieldAttr',
            'type': 2,
            'tableId': table_id,
            'viewId': view_id or '',
            'fieldId': field_id,
            'data': descriptor,
        }],
        'abnormal': False,
        'source': 1,
    }


_UI_TYPE_BY_CODE = {
    1: 'Text', 2: 'Number', 3: 'SingleSelect', 4: 'MultiSelect',
    5: 'DateTime', 7: 'Checkbox', 11: 'User', 15: 'Url', 17: 'Attachment',
    22: 'Location',
}


def _default_ui_type(type_code: int) -> str:
    return _UI_TYPE_BY_CODE.get(type_code, 'Text')


# ---------------------------------------------------------------------------
# HTTP submit
# ---------------------------------------------------------------------------

def submit_operations(cookies, base_token: str, table_id: str, operations: list,
                      member_id: int = None, local_rev: int = 0, req_id: int = 1) -> dict:
    """Submit an OT operation list to the server via HTTP rce/messages.

    Returns the raw JSON response body. Callers should check:
        resp['data']['type'] == 'ACCEPT_COMMIT'
        resp['data']['code'] == 0
    """
    if not operations:
        raise ValueError('submit_operations: operations must be non-empty')
    member_id = member_id or new_member_id()
    ops_json = json.dumps(operations, ensure_ascii=False)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb') as gz:
        gz.write(ops_json.encode('utf-8'))
    ops_gz = base64.b64encode(buf.getvalue()).decode('ascii')

    body = {
        'type': 'BITABLE_TABLE',
        'data': {
            'member_id': member_id,
            'user_ticket': '',
            'type': 'USER_CHANGES',
            'token': table_id,
            'lang': 'zh',
            'localRev': local_rev,
            'operations': ops_gz,
            'signature': str(uuid.uuid4()),
            'content_type': 'gzip/base64',
            'route_key': base_token,
        },
        'version': 2,
        'req_id': req_id,
        'context': {
            'os': 'mac',
            'os_version': '10.15.7',
            'app_version': '1.0.18.9709',
            'platform': 'web',
            'request_id': str(uuid.uuid4()),
        },
    }
    res = http_post_with_cookies(
        cookies, DOC_HOST, f'/space/api/rce/messages?member_id={member_id}', body,
    )
    resp = res.get('data') or {}
    if not isinstance(resp, dict):
        raise RuntimeError(
            f'USER_CHANGES failed: http_status={res.get("status")} '
            f'non_json_response={resp!r}'
        )
    top_code = resp.get('code')
    data = resp.get('data') or {}
    inner_code = data.get('code')
    resp_type = data.get('type')
    if top_code != 0 or inner_code != 0 or resp_type != 'ACCEPT_COMMIT':
        raise RuntimeError(
            f'USER_CHANGES failed: top_code={top_code} inner_code={inner_code} '
            f'type={resp_type} msg={resp.get("msg") or data.get("msg")} full={resp}'
        )
    return resp
