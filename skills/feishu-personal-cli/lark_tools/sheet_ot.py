"""
sheet_ot.py — OT operation builders + submitter for Feishu spreadsheets.

Wire format (verified by reverse-engineering 2026-05-15, see
references/api/rest_post_space_api_v2_sheet_user_changes.md):

  POST /space/api/v2/sheet/user_changes?token={obj_token}&member_id={mid}
  body: {
    base_rev:      int,                    # client's current revision
    content:       base64(gzip(protobuf)), # the operation(s)
    mode:          0,                      # always 0 (unverified other values)
    msg_timestamp: int,                    # client ms timestamp
    msg_id:        str,                    # UUID, idempotency key
    retryCount:    0,                      # always 0 on first send
  }

  protobuf inside `content`:
    Op := { f1=action_type, f2=sheet_id, f4=position?, f5=json_payload? }
    outer wrap: { f1=Op } or { f1=[Op...] } for composite commits

  action_type:
     4 = WRITE_CELL          f4={f1=row, f3=col}
     5 = INSERT_ROW          f4={f1=start_row, f2=row_count}
     6 = DELETE_ROW          f4={f1=start_row, f2=row_count}
     7 = INSERT_COLUMN       f4={f3=start_col, f4=col_count}
     8 = DELETE_COLUMN       f4={f3=start_col, f4=col_count}    (symmetric with action 7; verified 2026-05-18 via live test)
    20 = DELETE_SHEET        f5={"sheet_name":N, "index":I}
    21 = UPDATE_SHEET_PROPERTY f5={"origin_name":..., "sheet_name":..., "property_name":"name"}
    22 = CREATE_SHEET        f5={"sheet_id":..., "sheet_name":..., "index":I}
    61 = STYLE_RANGE         f5={"style":{...}, "target":{type:0, row, col, rowCount, colCount}}

Response: {"data":{"new_rev": N, "type": "ACCEPT_COMMIT", "edit_time": T}}

NOTE: Failure / conflict response not captured during RE. The submit_change
function below treats anything other than code=0 as a failure and raises.
"""

import base64
import datetime as _dt
import gzip
import json
import random
import re
import time
import uuid

from .config import DOC_HOST
from .http_utils import http_post_with_cookies
from .proto import encode_message


# ---------------------------------------------------------------------------
# Action type constants
# ---------------------------------------------------------------------------

WRITE_CELL = 4
INSERT_ROW = 5
DELETE_ROW = 6
INSERT_COLUMN = 7
DELETE_COLUMN = 8

DELETE_SHEET = 20
UPDATE_SHEET_PROPERTY = 21
CREATE_SHEET = 22
STYLE_RANGE = 61


# ---------------------------------------------------------------------------
# Default cell font (matches what browser sends on a fresh cell write)
# ---------------------------------------------------------------------------

_DEFAULT_FONT = (
    '10pt/1.5 MonospacedNumber, LarkHackSafariFont, LarkEmojiFont, '
    'LarkChineseQuote, -apple-system, BlinkMacSystemFont, "Helvetica Neue", '
    'Tahoma, "PingFang SC", "Microsoft Yahei", Arial, "Hiragino Sans GB", '
    'sans-serif, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", '
    '"Noto Color Emoji"'
)


def _default_style() -> dict:
    return {
        'font': _DEFAULT_FONT,
        'vAlign': 1,
        'hAlign': 3,
        'textDecoration': 0,
        'wordWrap': 0,
    }


# ---------------------------------------------------------------------------
# Op builders — each returns the inner protobuf {field: value} dict
# ---------------------------------------------------------------------------

# Excel 1900 epoch with the "1900-was-a-leap-year" off-by-one baked in:
# Excel treats 1900-02-29 as a real day, so for any date >= 1900-03-01 the
# serial is (date - 1899-12-30).days. Dates before 1900-03-01 are pre-quirk
# and never used in practice — we don't special-case them.
_EXCEL_EPOCH = _dt.datetime(1899, 12, 30)

# Auto-detected date / datetime patterns. Each entry: (regex, strptime_format,
# output_numberFormat). Order matters — most specific first so that the
# datetime patterns win over the bare-date pattern.
_DATE_PATTERNS = [
    # ISO 8601 with optional fractional seconds and TZ marker (TZ stripped,
    # treated as naive local time — matches how Excel / Feishu render dates).
    (re.compile(r'^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$'),
     'iso8601', 'yyyy-mm-dd hh:mm:ss'),
    (re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$'),
     '%Y-%m-%d %H:%M:%S', 'yyyy-mm-dd hh:mm:ss'),
    (re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$'),
     '%Y-%m-%d %H:%M', 'yyyy-mm-dd hh:mm'),
    (re.compile(r'^\d{4}-\d{2}-\d{2}$'),
     '%Y-%m-%d', 'yyyy-mm-dd'),
    (re.compile(r'^\d{4}/\d{1,2}/\d{1,2}$'),
     '%Y/%m/%d', 'yyyy/m/d'),
]


def _try_parse_date_string(s: str):
    """Return (excel_serial, numberFormat) if `s` is a recognized date /
    datetime string, else None. Pure dates (no time) yield an int serial;
    datetimes yield a float (day + fraction-of-day).
    """
    for pattern, strptime_fmt, output_fmt in _DATE_PATTERNS:
        if not pattern.match(s):
            continue
        try:
            if strptime_fmt == 'iso8601':
                # Strip TZ suffix and fractional seconds, then try seconds
                # then minute granularity.
                clean = re.sub(r'(?:Z|[+-]\d{2}:?\d{2})$', '', s)
                clean = re.sub(r'\.\d+', '', clean)
                try:
                    dt = _dt.datetime.strptime(clean, '%Y-%m-%dT%H:%M:%S')
                except ValueError:
                    dt = _dt.datetime.strptime(clean, '%Y-%m-%dT%H:%M')
            else:
                dt = _dt.datetime.strptime(s, strptime_fmt)
        except ValueError:
            return None
        delta = dt - _EXCEL_EPOCH
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and 'T' not in s and ':' not in s:
            return (delta.days, output_fmt)
        return (delta.days + delta.seconds / 86400, output_fmt)
    return None


def _coerce_cell_value(value, raw_string: bool = False):
    """Return ``(cell_value, auto_format_or_None)`` for op_write_cell.

    Coercion priority (a ↦ output cell type / behavior):
      * None / ''                        → ('', None)            empty cell
      * bool                             → ('True'|'False', None)  text (don't promote)
      * int / float                      → (value, None)           number cell
      * "YYYY-MM-DD" etc                 → (excel_serial, fmt)     date cell + autoFormatter
      * numeric-looking string ("46176") → (int(s) or float(s), None)  number cell
      * everything else                  → (str(value), None)       text cell

    `raw_string=True` short-circuits straight to text — for IDs / zip codes
    / phone numbers where a numeric or date-shaped string must stay literal.

    Date detection runs BEFORE numeric parsing so that "2026-05-11" beats
    int() (which would reject it for the dashes anyway) and "20260511"
    parses as a plain number (8-digit ID, not a compact date — no
    locale-implicit guesses).
    """
    if value is None:
        return ('', None)
    if isinstance(value, bool):  # bool isinstance int — guard first
        return (str(value), None)
    if isinstance(value, (int, float)):
        return (value, None)
    s = str(value)
    if raw_string or not s:
        return (s, None)
    # Date strings first — "2026-05-11" should not fall through to str.
    date_result = _try_parse_date_string(s)
    if date_result is not None:
        return date_result
    # Try int first to avoid promoting "5" to 5.0
    try:
        return (int(s), None)
    except ValueError:
        pass
    try:
        return (float(s), None)
    except ValueError:
        return (s, None)


def op_write_cell(sheet_id: str, row: int, col: int,
                  value=None, formula: str = None,
                  raw_string: bool = False) -> dict:
    """Write a single cell value or formula.

    Either `value` (str/number) or `formula` ("=A1+B2") may be set; if both,
    formula wins. For styling (bold / italic / color / etc.), follow up with
    op_style_range — value writes and style application are separate ops.

    Value type coercion (verified 2026-05-18 against browser capture):
    Feishu picks the cell type (number vs text) from the JSON value type in
    the payload — `"46176"` (string) yields a text cell, `46176` (number)
    yields a number cell. Number cells participate in `numberFormat` styling
    (date / percent / currency), text cells don't. So `op_write_cell` parses
    numeric-looking strings into actual numbers by default. Pass
    `raw_string=True` to keep a string verbatim (e.g. for zip codes,
    phone numbers with leading zeros).

    Date / datetime strings ("2026-05-11", "2026-05-11 14:30:00", ISO 8601)
    are auto-converted to Excel serial numbers, and `style.autoFormatter` is
    set to a matching display format (e.g. "yyyy-mm-dd"). This mirrors the
    browser's auto-detection captured 2026-05-18 — typing "2026-05-18" in a
    cell yields `value=46160` plus `autoFormatter.formatCached="yyyy/m/d"`.
    """
    style = _default_style()
    if formula is not None:
        payload = {'style': style}
        payload['formula'] = formula
        payload['formulaData'] = None  # server seems to accept missing AST
        payload['value'] = None
    else:
        cell_value, auto_format = _coerce_cell_value(value, raw_string=raw_string)
        if auto_format is not None:
            # Match browser shape: autoFormatter nested under style.
            style['autoFormatter'] = {'formatCached': auto_format}
        # Explicit newlines: bump wordWrap so the row height auto-expands and
        # the cell renders as multi-line. Without this the value is stored
        # correctly but Feishu clips it to a single line.
        if isinstance(cell_value, str) and '\n' in cell_value:
            style['wordWrap'] = 1
        payload = {'style': style, 'value': cell_value}
    payload['reminder'] = None
    payload['dataValidation'] = None
    return {
        1: WRITE_CELL,
        2: sheet_id,
        4: {1: int(row), 3: int(col)},
        5: json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
    }


def op_style_range(sheet_id: str, row: int, col: int, rowCount: int, colCount: int,
                   *,
                   bold: bool = None,
                   italic: bool = None,
                   underline: bool = None,
                   color: str = None,
                   bg_color: str = None,
                   font_size=None,
                   font_family: str = None,
                   h_align: int = None,
                   v_align: int = None,
                   number_format: str = None) -> dict:
    """Apply named styles to a rectangular range. At least one must be set.

    Each kwarg maps to one server-side field; passing None leaves that
    attribute alone (the server merges, not replaces). Field mappings
    reverse-engineered 2026-05-18:
      bold          → fontInfo.fontWeight (400/700)
      italic        → fontInfo.fontStyle ("italic" / null)
      underline     → textDecoration {active, flag: 1}
      color         → foreColor (hex string, e.g. "#FF0000")
      bg_color      → backColor (hex string)
      font_size     → fontInfo.fontSize ("14pt" — auto-appends 'pt' if missing)
      font_family   → fontInfo.fontFamily (e.g. "宋体-简")
      h_align       → hAlign enum: 0=left, 1=center, 2=right
      v_align       → vAlign enum: 0=top, 1=middle, 2=bottom
      number_format → formatter.formatCached (Excel format string, e.g. "0.00%")

    Border styling is not exposed here — the browser uses BULK_CONTENT_PATCH
    (action 74) with a positional style array for borders; that op has not
    been wrapped yet.
    """
    style = {}
    font_info = {}
    if bold is not None:
        font_info['fontWeight'] = 700 if bold else 400
    if italic is not None:
        font_info['fontStyle'] = 'italic' if italic else None
    if font_size is not None:
        s = str(font_size)
        font_info['fontSize'] = s if s.endswith('pt') else f'{s}pt'
    if font_family is not None:
        font_info['fontFamily'] = font_family
    if font_info:
        style['fontInfo'] = font_info
    if underline is not None:
        style['textDecoration'] = {'active': bool(underline), 'flag': 1}
    if color is not None:
        style['foreColor'] = color
    if bg_color is not None:
        style['backColor'] = bg_color
    if h_align is not None:
        style['hAlign'] = int(h_align)
    if v_align is not None:
        style['vAlign'] = int(v_align)
    if number_format is not None:
        style['formatter'] = {'formatCached': number_format}
    if not style:
        raise ValueError('op_style_range: no style attribute set')
    payload = {
        'style': style,
        'target': {
            'type': 0,
            'row': int(row), 'col': int(col),
            'rowCount': int(rowCount), 'colCount': int(colCount),
        },
    }
    return {
        1: STYLE_RANGE,
        2: sheet_id,
        5: json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
    }


def op_insert_row(sheet_id: str, start_row: int, count: int = 1) -> dict:
    return {1: INSERT_ROW, 2: sheet_id, 4: {1: int(start_row), 2: int(count)}}


def op_delete_row(sheet_id: str, start_row: int, count: int = 1) -> dict:
    return {1: DELETE_ROW, 2: sheet_id, 4: {1: int(start_row), 2: int(count)}}


def op_insert_column(sheet_id: str, start_col: int, count: int = 1) -> dict:
    return {1: INSERT_COLUMN, 2: sheet_id, 4: {3: int(start_col), 4: int(count)}}


def op_delete_column(sheet_id: str, start_col: int, count: int = 1) -> dict:
    """Symmetric with op_insert_column (action 7) — wire shape inferred from
    insert-col and verified 2026-05-18 by live test: commit accepts and the
    sheet's colCount drops by `count` afterward.
    """
    return {1: DELETE_COLUMN, 2: sheet_id, 4: {3: int(start_col), 4: int(count)}}


def op_create_sheet(sheet_id: str, sheet_name: str, index: int) -> dict:
    """Create a new sheet tab. Caller picks a 6-char hex sheet_id."""
    payload = {'sheet_id': sheet_id, 'sheet_name': sheet_name, 'index': int(index)}
    return {
        1: CREATE_SHEET,
        2: sheet_id,
        5: json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
    }


def op_delete_sheet(sheet_id: str, sheet_name: str, index: int) -> dict:
    payload = {'sheet_name': sheet_name, 'index': int(index)}
    return {
        1: DELETE_SHEET,
        2: sheet_id,
        5: json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
    }


def op_rename_sheet(sheet_id: str, origin_name: str, new_name: str) -> dict:
    payload = {
        'origin_name': origin_name,
        'sheet_name': new_name,
        'property_name': 'name',
    }
    return {
        1: UPDATE_SHEET_PROPERTY,
        2: sheet_id,
        5: json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
    }


# ---------------------------------------------------------------------------
# Encode + submit
# ---------------------------------------------------------------------------

def new_sheet_id() -> str:
    """Generate a 6-char hex sheet_id (matches the format observed in capture)."""
    return f'{random.randint(0, 0xFFFFFF):06x}'


def new_member_id() -> int:
    """Generate a fresh 14-digit client member ID for the user_changes URL param.

    NOTE: In a real browser, member_id is **stable for the session** and the
    server uses it for presence/cursor tracking. Each CLI commit currently
    generates a new value — this hasn't been observed to cause commits to be
    rejected, but does diverge from real-client behavior.

    TODO(presence-validation): If commits start getting rejected with a
    presence-related error, cache one value per spreadsheet_token in
    sheet_state.py instead of calling this fresh each commit. Be aware that
    presence validation, if/when it lands, will likely also require a real WS
    COLLABROOM WATCH + ws_ticket — so fixing only member_id may not be enough.
    """
    return random.randint(10**13, 10**14 - 1)


def pack_content(ops) -> str:
    """Encode one op-dict (or a list of op-dicts) → base64(gzip(protobuf))."""
    if isinstance(ops, dict):
        outer = {1: ops}
    else:
        outer = {1: list(ops)}
    raw = encode_message(outer)
    return base64.b64encode(gzip.compress(raw)).decode('ascii')


def submit_change(cookies, spreadsheet_token: str, member_id: int,
                  ops, base_rev: int) -> dict:
    """POST one OT commit to user_changes. Returns the parsed response data dict.

    On success, returns {'new_rev': N, 'type': 'ACCEPT_COMMIT', 'edit_time': T}.
    On non-zero code, raises RuntimeError with the server message.
    """
    body = {
        'base_rev': int(base_rev),
        'content': pack_content(ops),
        'mode': 0,
        'msg_timestamp': int(time.time() * 1000),
        'msg_id': str(uuid.uuid4()),
        'retryCount': 0,
    }
    url_path = (
        f'/space/api/v2/sheet/user_changes'
        f'?token={spreadsheet_token}&member_id={member_id}'
    )
    res = http_post_with_cookies(cookies, DOC_HOST, url_path, body)
    if not isinstance(res, dict) or not isinstance(res.get('data'), dict):
        raise RuntimeError(f'user_changes: unexpected response: {res!r}')
    payload = res['data']
    if payload.get('code') != 0:
        raise RuntimeError(
            f"user_changes failed: code={payload.get('code')} "
            f"msg={payload.get('msg')!r} (base_rev={base_rev})"
        )
    data = payload.get('data') or {}
    if 'new_rev' not in data:
        raise RuntimeError(f'user_changes: missing new_rev in response: {payload!r}')
    return data
