"""
commands/bitable_write.py - Bitable write commands.

Implements CRUD over the HTTP OT-submit path discovered empirically:
POST /space/api/rce/messages with outer type "BITABLE_TABLE" accepts the
same OT operations payload as the WebSocket main channel, avoiding the
need for a WS client entirely.

Commands:
    bitable create [title]
    bitable set-record    <token|url> <tableId> <recordId> <fieldName=value>...
    bitable add-record    <token|url> <tableId> <fieldName=value>...
    bitable delete-record <token|url> <tableId> <recordId>
    bitable add-field     <token|url> <tableId> <name> [--type text|number|...]
    bitable add-fields-batch <token|url> <tableId> <fields_json>
    bitable set-field-format <token|url> <tableId> <fieldId|fieldName> <format>
    bitable rename-field  <token|url> <tableId> <fieldId> <new_name>
"""

from __future__ import annotations

import json
import sys
import time
from typing import Optional

from ..config import DOC_HOST
from ..http_utils import http_post_with_cookies
from .doc_write import _lookup_space_id
from ..bitable_ot import (
    submit_operations, new_record_id, new_field_id,
    op_set_record, op_add_record, op_delete_record,
    op_add_field, op_rename_field,
    encode_value_for_field, encode_text_value,
)
from ..bitable_base_ot import (
    build_add_table_operation,
    submit_base_operations,
    new_member_id as new_base_member_id,
)
from .bitable import (
    resolve_bitable_token, resolve_wiki_to_bitable,
    fetch_bitable_schema, build_field_map,
    _bitable_post_with_csrf_fallback,
)


# ---------------------------------------------------------------------------
# Phase 1: create bitable
# ---------------------------------------------------------------------------

def create_bitable(cookies, title: str = '', time_zone: str = 'Asia/Shanghai',
                   parent_wiki_token: Optional[str] = None) -> dict:
    """Create a new bitable in the user's wiki.

    If `parent_wiki_token` is given, the new bitable is placed as a child
    of that wiki node. Otherwise it lands at the root of the personal wiki.
    """
    if parent_wiki_token:
        # Child-creation payload (verified 2026-05-06)
        payload = {
            'space_id': _lookup_space_id(cookies, parent_wiki_token),
            'parent_wiki_token': parent_wiki_token,
            'ua_type': 'Web',
            'scene': 'wiki_create',
            'obj_type': 8,
            'node_type': 0,
            'synergy_uuid': str(int(time.time() * 1000)),
            'template_token': '',
            'time_zone': time_zone,
            'ext_info': {'platform': 'web'},   # new-format: object, not stringified
        }
        if title:
            payload['title'] = title
    else:
        # Root-creation payload (legacy capture)
        payload = {
            'title': title or '',
            'ua_type': 'Web',
            'scene': 'wiki_create',
            'node_type': 0,
            'obj_type': 8,
            'time_zone': time_zone,
            'ext_info_str': '{"platform":"web"}',
        }
    res = http_post_with_cookies(
        cookies, DOC_HOST, '/space/api/wiki/v2/tree/create_node/', payload,
    )
    body = res.get('data') or {}
    if body.get('code') != 0:
        raise RuntimeError(
            f'create_node failed: code={body.get("code")} msg={body.get("msg")} body={body}'
        )
    return body.get('data') or {}


def cmd_bitable_create(cookies, title: str, time_zone: Optional[str] = None,
                       parent_wiki_token: Optional[str] = None):
    node = create_bitable(cookies, title, time_zone or 'Asia/Shanghai',
                          parent_wiki_token=parent_wiki_token)
    obj_token = node.get('obj_token')
    wiki_token = node.get('wiki_token')
    url = node.get('url') or f'https://{DOC_HOST}/wiki/{wiki_token}'
    print(json.dumps({
        'success': True,
        'title': title,
        'obj_token': obj_token,
        'wiki_token': wiki_token,
        'url': url,
    }, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Token resolution (shared by all write commands)
# ---------------------------------------------------------------------------

def _resolve(cookies, token_or_url: str) -> str:
    """Return the bitable base obj_token, resolving wiki if needed."""
    parsed = resolve_bitable_token(token_or_url)
    token = parsed['token']
    if parsed['is_wiki']:
        token = resolve_wiki_to_bitable(cookies, token)
    return token


def _load_field_map(cookies, base_token: str, table_id: str) -> dict:
    """Return {fieldId: {name, type, optionMap}} for a table.

    Uses the CSRF-fixed tablesv3 POST path.  On failure returns an empty
    dict so writes can still proceed with raw field IDs.
    """
    try:
        schema = fetch_bitable_schema(cookies, base_token, table_id)
        return build_field_map(schema)
    except Exception as exc:
        print(
            f"[bitable_write] Could not load field map (tablesv3 POST failed): {exc}",
            file=sys.stderr,
        )
    try:
        return _load_field_map_from_clientvars(cookies, base_token, table_id)
    except Exception as exc:
        print(
            f"[bitable_write] Could not load field map from clientvars: {exc}",
            file=sys.stderr,
        )
        return {}


def _load_field_map_from_clientvars(cookies, base_token: str, table_id: str) -> dict:
    """Load table field map from clientvars as a fallback to tablesv3."""
    import base64, zlib
    from ..config import BITABLE_HOST
    from ..http_utils import http_get

    clientvars_url = (
        f'/space/api/v1/bitable/{base_token}/clientvars'
        f'?tableID={table_id}&viewID=&recordLimit=0&ondemandLimit=0'
        f'&needBase=true&viewLazyLoad=false&ondemandVer=2&openType=1'
        f'&noMissCS=true&optimizationFlag=1&removeFmlExtra=true'
    )
    res = http_get(cookies, BITABLE_HOST, clientvars_url)
    payload = res.get('data')
    if not isinstance(payload, dict) or payload.get('code') != 0:
        raise RuntimeError(f"Failed to fetch clientvars: payload={payload!r}")
    table_str = (payload.get('data') or {}).get('table')
    if not table_str:
        raise RuntimeError('No table field in clientvars response')
    table_data = json.loads(zlib.decompress(base64.b64decode(table_str), 47))
    return build_field_map({'data': {'table': table_data}})


def _load_field_descriptor(cookies, base_token: str, table_id: str, field_id: str) -> dict:
    """Return the full raw FieldDescriptor for a field, or None."""
    try:
        schema = fetch_bitable_schema(cookies, base_token, table_id)
    except Exception:
        return None
    fm = ((schema.get('data') or {}).get('table') or {}).get('fieldMap') or {}
    return fm.get(field_id)


def _load_table_layout(cookies, base_token: str, table_id: str) -> dict:
    """Fetch table layout and return {field_count, view_ids}.

    Tries tablesv3 POST (with CSRF fix).  On failure falls back to a
    minimal layout derived from clientvars so add-field can still proceed.
    """
    import base64, zlib
    from ..config import BITABLE_HOST

    # Try tablesv3 POST first (with CSRF fallback)
    try:
        res = _bitable_post_with_csrf_fallback(
            cookies, BITABLE_HOST,
            f'/space/api/bitable/{base_token}/tablesv3/',
            {
                'tableIDList': [table_id],
                'tablePartitionFlagList': [0],
                'tablePartitionForNoRankFlagList': [],
                'encodingProtocol': {'compression': 1, 'serialization': 0},
            },
        )
        encoded = (res.get('data') or {}).get('data', {}).get(table_id)
        if encoded:
            table_data = json.loads(zlib.decompress(base64.b64decode(encoded), 47))
            field_count = len(table_data.get('fieldMap') or {})
            view_ids = list(table_data.get('views') or [])
            if view_ids:
                return {'field_count': field_count, 'view_ids': view_ids}
    except Exception as exc:
        print(
            f"[bitable_write] tablesv3 POST failed, falling back to clientvars: {exc}",
            file=sys.stderr,
        )

    # Fallback: derive layout from clientvars GET (table-level view info)
    from ..http_utils import http_get
    clientvars_url = (
        f'/space/api/v1/bitable/{base_token}/clientvars'
        f'?tableID={table_id}&viewID=&recordLimit=0&needBase=true'
        f'&viewLazyLoad=true&ondemandVer=2&openType=1'
    )
    res = http_get(cookies, BITABLE_HOST, clientvars_url)
    payload = res.get('data')
    if not isinstance(payload, dict) or payload.get('code') != 0:
        raise RuntimeError(f"Failed to fetch clientvars for layout: payload={payload!r}")
    client_data = payload.get('data') or {}
    base_str = client_data.get('base')
    if not base_str:
        raise RuntimeError('No base field in clientvars response')
    base_json = json.loads(zlib.decompress(base64.b64decode(base_str), 47))
    # The base JSON's top-level 'views' array contains view IDs for the
    # current table when tableID is specified.
    view_ids = base_json.get('views') or []
    if not view_ids:
        raise RuntimeError(
            f'Cannot determine view_ids for table {table_id}. '
            'The table may have no views. Please open the base in Feishu first.'
        )
    field_count = len(base_json.get('fieldMap') or {})

    return {'field_count': field_count, 'view_ids': view_ids}


def _resolve_field_name_to_id(field_map: dict, name_or_id: str) -> tuple:
    """Return (field_id, field_info) given a name or id.

    If ``name_or_id`` is already a field ID (starts with ``fld``), return
    it directly — no schema lookup needed.  This allows writing records
    without a successful tablesv3 POST.
    """
    if name_or_id in field_map:
        return name_or_id, field_map[name_or_id]
    # Direct field ID passthrough (e.g. "fld14Vw7y1")
    if name_or_id.startswith('fld') and len(name_or_id) >= 10:
        return name_or_id, {'name': name_or_id, 'type': 1}
    # match by name
    for fid, info in field_map.items():
        if info.get('name') == name_or_id:
            return fid, info
    # If field_map is empty (tablesv3 failed), treat as raw field ID
    if not field_map and name_or_id.startswith('fld'):
        return name_or_id, {'name': name_or_id, 'type': 1}
    known = [i.get("name", fid) for fid, i in field_map.items()]
    raise RuntimeError(
        f'Field not found: {name_or_id!r}. Known fields: {known}'
    )


def _parse_kv_pairs(pairs: list) -> dict:
    """Parse ["name=value", "other=x"] into {"name": "value", "other": "x"}."""
    out = {}
    for p in pairs:
        if '=' not in p:
            raise ValueError(f'Expected name=value, got: {p!r}')
        k, v = p.split('=', 1)
        out[k.strip()] = v
    return out


def _build_cell_data(field_map: dict, kv: dict) -> dict:
    """Translate user-supplied {fieldName: rawValue} into OT cell_data."""
    cell_data = {}
    for name, raw in kv.items():
        fid, info = _resolve_field_name_to_id(field_map, name)
        ftype = int(info.get('type') or 1)
        value = raw
        # Type-specific parsing of the raw string.
        if ftype == 2:  # number-like
            try:
                value = float(raw)
                if value.is_integer():
                    value = int(value)
            except (TypeError, ValueError):
                pass
        elif ftype == 5:  # datetime — accept unix ms int or ISO date
            try:
                value = int(raw)
            except (TypeError, ValueError):
                from datetime import datetime
                dt = datetime.fromisoformat(str(raw))
                value = int(dt.timestamp() * 1000)
        elif ftype == 7:  # checkbox
            value = str(raw).lower() in ('1', 'true', 'yes', 'y', 't')
        elif ftype == 3:  # single select — map name → opt_id
            om = info.get('optionMap') or {}
            name_to_id = {n: i for i, n in om.items()}
            value = name_to_id.get(str(raw), raw)
        elif ftype == 4:  # multi select — comma-separated names → ids
            om = info.get('optionMap') or {}
            name_to_id = {n: i for i, n in om.items()}
            parts = [p.strip() for p in str(raw).split(',') if p.strip()]
            value = [name_to_id.get(p, p) for p in parts]
        cell_data[fid] = encode_value_for_field(ftype, value)
    return cell_data


# ---------------------------------------------------------------------------
# Record CRUD commands
# ---------------------------------------------------------------------------

def cmd_bitable_set_record(cookies, token_or_url: str, table_id: str, record_id: str, kv_pairs: list):
    base_token = _resolve(cookies, token_or_url)
    field_map = _load_field_map(cookies, base_token, table_id)
    kv = _parse_kv_pairs(kv_pairs)
    cell_data = _build_cell_data(field_map, kv)
    op = op_set_record(table_id, record_id, cell_data)
    resp = submit_operations(cookies, base_token, table_id, [op])
    print(json.dumps({
        'success': True,
        'obj_token': base_token,
        'table_id': table_id,
        'record_id': record_id,
        'rev': (resp.get('data') or {}).get('rev'),
        'updated': list(kv.keys()),
    }, indent=2, ensure_ascii=False))


def cmd_bitable_add_record(cookies, token_or_url: str, table_id: str, kv_pairs: list):
    base_token = _resolve(cookies, token_or_url)
    field_map = _load_field_map(cookies, base_token, table_id)
    kv = _parse_kv_pairs(kv_pairs)
    cell_data = _build_cell_data(field_map, kv)
    new_rid = new_record_id()
    op = op_add_record(table_id, new_rid, cell_data)
    resp = submit_operations(cookies, base_token, table_id, [op])
    print(json.dumps({
        'success': True,
        'obj_token': base_token,
        'table_id': table_id,
        'record_id': new_rid,
        'rev': (resp.get('data') or {}).get('rev'),
        'fields': list(kv.keys()),
    }, indent=2, ensure_ascii=False))


def cmd_bitable_add_records_batch(cookies, token_or_url: str, table_id: str,
                                   records: list[dict[str, str]],
                                   resolve_field_names: bool = True):
    """Add multiple records in a single OT operation.

    Standard workflow (avoids tablesv3 POST entirely):
      1. Create base → get obj_token
      2. clientvars GET → get table_id
      3. rce/messages AddField × N → get field IDs
      4. rce/messages AddRecordsV2 (batch) → write all records at once
      5. records GET → verify

    Each record dict maps field ID (``fldXXXXXXXXXX``) → value string.
    No schema lookup is performed — values are encoded as text (type 1).
    Pass field IDs directly from the AddField responses.
    """
    base_token = _resolve(cookies, token_or_url)
    field_map = _load_field_map(cookies, base_token, table_id) if resolve_field_names else {}
    operations = []
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise RuntimeError(f'Record #{index} is not an object: {record!r}')
        try:
            cell_data = _build_cell_data(field_map, record)
        except RuntimeError as exc:
            raise RuntimeError(f'Invalid record #{index}: {exc}') from exc
        new_rid = new_record_id()
        operations.append(op_add_record(table_id, new_rid, cell_data))

    if not operations:
        print(json.dumps({'success': False, 'error': 'No records provided'}))
        return

    resp = submit_operations(cookies, base_token, table_id, operations)
    rev = (resp.get('data') or {}).get('rev', '?')
    print(json.dumps({
        'success': True,
        'obj_token': base_token,
        'table_id': table_id,
        'records_written': len(operations),
        'rev': rev,
    }, indent=2, ensure_ascii=False))


def cmd_bitable_delete_record(cookies, token_or_url: str, table_id: str, record_id: str):
    base_token = _resolve(cookies, token_or_url)
    op = op_delete_record(table_id, record_id)
    resp = submit_operations(cookies, base_token, table_id, [op])
    print(json.dumps({
        'success': True,
        'obj_token': base_token,
        'table_id': table_id,
        'record_id': record_id,
        'rev': (resp.get('data') or {}).get('rev'),
    }, indent=2, ensure_ascii=False))


def cmd_bitable_delete_records_batch(cookies, token_or_url: str, table_id: str, record_ids: list[str]):
    """Delete multiple records in one OT submit."""
    base_token = _resolve(cookies, token_or_url)
    clean_ids = [str(record_id).strip() for record_id in record_ids if str(record_id).strip()]
    if not clean_ids:
        print(json.dumps({'success': False, 'error': 'No record IDs provided'}))
        return
    operations = [op_delete_record(table_id, record_id) for record_id in clean_ids]
    resp = submit_operations(cookies, base_token, table_id, operations)
    print(json.dumps({
        'success': True,
        'obj_token': base_token,
        'table_id': table_id,
        'records_deleted': len(clean_ids),
        'record_ids': clean_ids,
        'rev': (resp.get('data') or {}).get('rev'),
    }, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Field CRUD commands (Phase 3)
# ---------------------------------------------------------------------------

_FIELD_TYPE_ALIASES = {
    'text': 1, 'number': 2, 'num': 2, 'int': 2, 'float': 2,
    'singleselect': 3, 'single': 3,
    'multiselect': 4, 'multi': 4,
    'datetime': 5, 'date': 5,
    'checkbox': 7, 'bool': 7,
    'user': 11, 'url': 15, 'attachment': 17, 'location': 22,
}


def _field_property_for_type(ftype: int, *, number_format: str | None = None) -> dict:
    if ftype != 2:
        return {}
    formatter = (number_format or '0.##########').strip()
    return {'formatter': formatter} if formatter else {}


def _normalize_field_spec(field: dict | str, index: int) -> dict:
    """Normalize a batch field spec into name/type/format values."""
    if isinstance(field, str):
        name = field.strip()
        type_name = 'text'
        number_format = None
    elif isinstance(field, dict):
        name = str(field.get('name') or field.get('field') or '').strip()
        type_name = str(field.get('type') or field.get('type_name') or 'text').strip() or 'text'
        number_format = field.get('format')
        if number_format is None:
            number_format = field.get('number_format')
        number_format = str(number_format).strip() if number_format is not None else None
    else:
        raise ValueError(f'Field spec #{index} must be an object or string, got: {field!r}')
    if not name:
        raise ValueError(f'Field spec #{index} is missing a non-empty name')
    ftype = _FIELD_TYPE_ALIASES.get(type_name.lower())
    if ftype is None:
        raise ValueError(f'Unknown field type for {name!r}: {type_name!r}. Valid: {", ".join(sorted(_FIELD_TYPE_ALIASES))}')
    return {
        'name': name,
        'type_name': type_name,
        'type': ftype,
        'format': number_format,
    }


def cmd_bitable_add_field(
    cookies,
    token_or_url: str,
    table_id: str,
    name: str,
    type_name: str = 'text',
    number_format: str | None = None,
):
    """Append a new field at the end of every view in the table.

    Reads the current field count + view list from tablesv3, then submits
    an AddField op with `indexes` and `total` populated (per ot_protocol §4.1.2).
    """
    base_token = _resolve(cookies, token_or_url)
    ftype = _FIELD_TYPE_ALIASES.get((type_name or 'text').lower())
    if ftype is None:
        raise ValueError(f'Unknown field type: {type_name!r}. Valid: {", ".join(sorted(_FIELD_TYPE_ALIASES))}')
    layout = _load_table_layout(cookies, base_token, table_id)
    new_fid = new_field_id()
    total_after = layout['field_count'] + 1
    op = op_add_field(
        table_id=table_id,
        field_id=new_fid,
        name=name,
        field_type=ftype,
        view_ids=layout['view_ids'],
        total_after=total_after,
        property_obj=_field_property_for_type(ftype, number_format=number_format),
    )
    resp = submit_operations(cookies, base_token, table_id, [op])
    print(json.dumps({
        'success': True,
        'obj_token': base_token,
        'table_id': table_id,
        'field_id': new_fid,
        'name': name,
        'type': ftype,
        'format': number_format if ftype == 2 and number_format else ('0.##########' if ftype == 2 else None),
        'total_fields': total_after,
        'views': layout['view_ids'],
        'rev': (resp.get('data') or {}).get('rev'),
    }, indent=2, ensure_ascii=False))


def cmd_bitable_add_fields_batch(
    cookies,
    token_or_url: str,
    table_id: str,
    fields: list[dict | str],
):
    """Append multiple fields with one schema read and one OT submit."""
    if not isinstance(fields, list):
        raise ValueError('fields_json must be a JSON array')
    base_token = _resolve(cookies, token_or_url)
    layout = _load_table_layout(cookies, base_token, table_id)
    operations = []
    added = []
    for index, raw_field in enumerate(fields, 1):
        spec = _normalize_field_spec(raw_field, index)
        new_fid = new_field_id()
        total_after = layout['field_count'] + index
        op = op_add_field(
            table_id=table_id,
            field_id=new_fid,
            name=spec['name'],
            field_type=spec['type'],
            view_ids=layout['view_ids'],
            total_after=total_after,
            property_obj=_field_property_for_type(spec['type'], number_format=spec['format']),
        )
        operations.append(op)
        added.append({
            'field_id': new_fid,
            'name': spec['name'],
            'type': spec['type'],
            'format': spec['format'] if spec['type'] == 2 and spec['format'] else ('0.##########' if spec['type'] == 2 else None),
            'total_fields': total_after,
        })

    if not operations:
        print(json.dumps({'success': False, 'error': 'No fields provided'}))
        return

    resp = submit_operations(cookies, base_token, table_id, operations)
    print(json.dumps({
        'success': True,
        'obj_token': base_token,
        'table_id': table_id,
        'fields_added': len(added),
        'fields': added,
        'views': layout['view_ids'],
        'rev': (resp.get('data') or {}).get('rev'),
    }, indent=2, ensure_ascii=False))


def cmd_bitable_rename_field(cookies, token_or_url: str, table_id: str, field_id_or_name: str, new_name: str):
    base_token = _resolve(cookies, token_or_url)
    # Allow passing the current name instead of field_id.
    field_map = _load_field_map(cookies, base_token, table_id)
    fid, _ = _resolve_field_name_to_id(field_map, field_id_or_name)
    # Fetch the full FieldDescriptor so we preserve property / type / ui_type.
    descriptor = _load_field_descriptor(cookies, base_token, table_id, fid)
    if descriptor is None:
        raise RuntimeError(f'Field {fid} not found in table {table_id}')
    old_name = descriptor.get('name')
    op = op_rename_field(table_id, fid, new_name, current_descriptor=descriptor)
    resp = submit_operations(cookies, base_token, table_id, [op])
    print(json.dumps({
        'success': True,
        'obj_token': base_token,
        'table_id': table_id,
        'field_id': fid,
        'old_name': old_name,
        'new_name': new_name,
        'rev': (resp.get('data') or {}).get('rev'),
    }, indent=2, ensure_ascii=False))


def cmd_bitable_set_field_format(
    cookies,
    token_or_url: str,
    table_id: str,
    field_id_or_name: str,
    number_format: str,
):
    base_token = _resolve(cookies, token_or_url)
    field_map = _load_field_map(cookies, base_token, table_id)
    fid, _ = _resolve_field_name_to_id(field_map, field_id_or_name)
    descriptor = _load_field_descriptor(cookies, base_token, table_id, fid)
    if descriptor is None:
        raise RuntimeError(f'Field {fid} not found in table {table_id}')
    if int(descriptor.get('type') or 0) != 2:
        raise RuntimeError(f'Field {fid} is not a number field')
    old_name = descriptor.get('name') or fid
    old_format = (descriptor.get('property') or {}).get('formatter')
    descriptor = dict(descriptor)
    descriptor['property'] = dict(descriptor.get('property') or {})
    descriptor['property']['formatter'] = number_format
    op = op_rename_field(table_id, fid, old_name, current_descriptor=descriptor)
    resp = submit_operations(cookies, base_token, table_id, [op])
    print(json.dumps({
        'success': True,
        'obj_token': base_token,
        'table_id': table_id,
        'field_id': fid,
        'name': old_name,
        'old_format': old_format,
        'format': number_format,
        'rev': (resp.get('data') or {}).get('rev'),
    }, indent=2, ensure_ascii=False))


def cmd_bitable_add_table(
    cookies,
    token_or_url: str,
    name: str,
    *,
    user_ticket: str,
    owner_user_id: str,
    owner_name: str,
    owner_en_name: str,
    owner_avatar: str = "",
    local_rev: int = 0,
    member_id: int | None = None,
    table_index: int = 1,
):
    base_token = _resolve(cookies, token_or_url)
    member_id = member_id or new_base_member_id()
    table_id, field_id, view_id, operations = build_add_table_operation(
        name=name,
        index=table_index,
        owner_user_id=owner_user_id,
        owner_name=owner_name,
        owner_en_name=owner_en_name,
        owner_avatar=owner_avatar,
    )
    resp = submit_base_operations(
        cookies,
        base_token=base_token,
        operations=operations,
        user_ticket=user_ticket,
        member_id=member_id,
        local_rev=local_rev,
    )
    print(json.dumps({
        "success": True,
        "obj_token": base_token,
        "table_id": table_id,
        "field_id": field_id,
        "view_id": view_id,
        "name": name,
        "member_id": member_id,
        "rev": (resp.get("data") or {}).get("rev"),
    }, indent=2, ensure_ascii=False))
