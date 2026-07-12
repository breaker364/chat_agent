"""
commands/sheet_write.py — high-level CLI handlers for spreadsheet writes.

Pairs with commands/sheet.py (read side). All write operations go through the
OT user_changes endpoint via lark_tools.sheet_ot. base_rev is auto-tracked
in ~/.lark_state.json (lark_tools.sheet_state).

CLI surface (wired in cli.py):
  lark sheet create     [--title T] [--parent-wiki TOKEN] [--space SPACE_ID]
  lark sheet set-cell   <url> <range> <value>      [--sheet NAME] [--style JSON]
  lark sheet set-formula <url> <cell> <formula>    [--sheet NAME]
  lark sheet set-range  <url> <start> <values_json> [--style JSON] [--sheet NAME]
  lark sheet set-style  <url> <range> --style JSON [--sheet NAME]
  lark sheet insert-row <url> --at N [--count N]   [--sheet NAME]
  lark sheet delete-row <url> --at N [--count N]   [--sheet NAME]
  lark sheet insert-col <url> --at A [--count N]   [--sheet NAME]
  lark sheet delete-col <url> --at A [--count N]   [--sheet NAME]
  lark sheet add-tab    <url> --name NAME [--at N]
  lark sheet delete-tab <url> --name NAME
  lark sheet rename-tab <url> --from OLD --to NEW
  lark sheet state-clear [--token TOKEN]
"""

import json
import re
import sys

from ..config import DOC_HOST
from ..http_utils import http_post_with_cookies
from .. import sheet_ot, sheet_state
from .sheet import fetch_sheet_list, resolve_sheet_token


# ---------------------------------------------------------------------------
# Range parsing — A1 / A1:B3 / 1 (row) / A (column)
# ---------------------------------------------------------------------------

_CELL_RE = re.compile(r'^([A-Z]+)(\d+)$')


def _col_letters_to_index(letters: str) -> int:
    """A->0, Z->25, AA->26, ..."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord('A') + 1)
    return n - 1


def _parse_cell(s: str) -> tuple:
    """'A1' → (row=0, col=0). Raises on invalid input."""
    m = _CELL_RE.match(s.strip().upper())
    if not m:
        raise ValueError(f'invalid cell ref: {s!r} (expected like A1, B12, AA3)')
    col = _col_letters_to_index(m.group(1))
    row = int(m.group(2)) - 1
    if row < 0:
        raise ValueError(f'row must be >= 1: {s!r}')
    return row, col


def parse_range(s: str) -> dict:
    """Parse 'A1' / 'A1:B3' → {row, col, rowCount, colCount}."""
    s = s.strip().upper()
    if ':' in s:
        a, b = s.split(':', 1)
        r1, c1 = _parse_cell(a)
        r2, c2 = _parse_cell(b)
        if r2 < r1 or c2 < c1:
            r1, r2 = sorted((r1, r2))
            c1, c2 = sorted((c1, c2))
        return {'row': r1, 'col': c1, 'rowCount': r2 - r1 + 1, 'colCount': c2 - c1 + 1}
    r, c = _parse_cell(s)
    return {'row': r, 'col': c, 'rowCount': 1, 'colCount': 1}


def parse_col_letter(s: str) -> int:
    """'A' → 0, 'B' → 1, ..."""
    s = s.strip().upper()
    if not re.match(r'^[A-Z]+$', s):
        raise ValueError(f'invalid column letter: {s!r}')
    return _col_letters_to_index(s)


# ---------------------------------------------------------------------------
# Sheet resolution
# ---------------------------------------------------------------------------

def _resolve(cookies, url_or_token: str, sheet_name: str = None) -> tuple:
    """Resolve URL/token + optional sheet name → (spreadsheet_token, sheet_id, sheet_name, sheet_index, all_sheets).

    sheet_name=None defaults to the first sheet.
    """
    info = resolve_sheet_token(cookies, url_or_token)
    spreadsheet_token = info['spreadsheetToken']
    sheets = fetch_sheet_list(cookies, spreadsheet_token)
    if not sheets:
        raise RuntimeError('No sheets found in spreadsheet')
    if sheet_name:
        for i, s in enumerate(sheets):
            if s['name'] == sheet_name or s['sheetId'] == sheet_name:
                return spreadsheet_token, s['sheetId'], s['name'], i, sheets
        avail = ', '.join(s['name'] for s in sheets)
        raise RuntimeError(f'sheet not found: {sheet_name!r}. Available: {avail}')
    s = sheets[0]
    return spreadsheet_token, s['sheetId'], s['name'], 0, sheets


def _commit(cookies, spreadsheet_token: str, ops, *, label: str) -> int:
    """Submit one OT commit using cached base_rev. Returns new_rev."""
    base_rev = sheet_state.get_rev(spreadsheet_token, default=0)
    member_id = sheet_ot.new_member_id()
    print(f'[sheet] {label} — base_rev={base_rev} member_id={member_id}', file=sys.stderr)
    try:
        result = sheet_ot.submit_change(cookies, spreadsheet_token, member_id, ops, base_rev)
    except RuntimeError as e:
        if 'base_rev' in str(e).lower() or 'reject' in str(e).lower():
            print(
                f'[sheet] HINT: cached base_rev={base_rev} may be stale. '
                f'Try: lark sheet state-clear --token {spreadsheet_token}',
                file=sys.stderr,
            )
        raise
    new_rev = int(result['new_rev'])
    sheet_state.set_rev(spreadsheet_token, new_rev)
    return new_rev


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# create — POST wiki/v2/tree/create_node with obj_type=3
# ---------------------------------------------------------------------------

def cmd_sheet_create(cookies, opts: dict = None):
    opts = opts or {}
    xlsx_path = opts.get('fromXlsx')
    if xlsx_path:
        # xlsx import takes a different 5-step path (see sheet_xlsx_import).
        # Requires --parent-wiki because drive's import endpoint needs a
        # destination node to attach the resulting wiki sheet to.
        parent = opts.get('parentWiki')
        if not parent:
            raise ValueError('--from-xlsx requires --parent-wiki <TOKEN>')
        from .. import sheet_xlsx_import
        result = sheet_xlsx_import.import_xlsx_to_sheet(
            cookies, xlsx_path, parent, title=opts.get('title'),
        )
        _emit(result)
        return
    body = {
        'title': opts.get('title') or '',
        'ua_type': 'Web',
        'scene': 'wiki_create',
        'node_type': 0,
        'obj_type': 3,
    }
    parent = opts.get('parentWiki')
    space = opts.get('spaceId')
    if parent:
        body['parent_wiki_token'] = parent
    if space:
        body['space_id'] = space
    if parent or space:
        # Subdirectory creation typically also wants synergy_uuid + template_token
        # (see references/api/rest_post_space_api_wiki_v2_tree_create_node.md)
        import time
        body['synergy_uuid'] = str(int(time.time() * 1000))
        body['template_token'] = ''

    res = http_post_with_cookies(
        cookies, DOC_HOST, '/space/api/wiki/v2/tree/create_node/', body,
    )
    payload = res.get('data') if isinstance(res, dict) else None
    if not payload or payload.get('code') != 0:
        raise RuntimeError(f'create_node failed: {payload!r}')
    data = payload.get('data') or {}
    out = {
        'spreadsheetToken': data.get('obj_token'),
        'wikiToken': data.get('wiki_token'),
        'url': data.get('url'),
        'sheetsUrl': f"https://{DOC_HOST}/sheets/{data.get('obj_token')}",
        'spaceId': data.get('space_id'),
        'title': data.get('title'),
    }
    _emit(out)


# ---------------------------------------------------------------------------
# set-cell — write a cell value (string or number)
# ---------------------------------------------------------------------------

def cmd_sheet_set_cell(cookies, url: str, range_str: str, value: str, opts: dict = None):
    """Write a single cell. Optional --style takes the AI-friendly schema
    (same as set-range / set-style).
    """
    opts = opts or {}
    spreadsheet_token, sheet_id, sheet_name, _, _ = _resolve(cookies, url, opts.get('sheetName'))
    rng = parse_range(range_str)
    if rng['rowCount'] != 1 or rng['colCount'] != 1:
        raise ValueError('set-cell requires single-cell range; use set-formula or batch for ranges')

    ops = [sheet_ot.op_write_cell(
        sheet_id, rng['row'], rng['col'], value=value,
        raw_string=bool(opts.get('raw_string')),
    )]

    # Optional rich style — translated to STYLE_RANGE op for the same cell.
    style_applied = None
    style_json = opts.get('style')
    if style_json:
        try:
            ai_style = json.loads(style_json) if isinstance(style_json, str) else style_json
        except json.JSONDecodeError as e:
            raise ValueError(f'invalid --style JSON: {e}')
        style_kwargs = _ai_style_to_kwargs(ai_style)
        if style_kwargs:
            ops.append(sheet_ot.op_style_range(
                sheet_id, rng['row'], rng['col'], 1, 1, **style_kwargs,
            ))
            style_applied = ai_style

    new_rev = _commit(
        cookies, spreadsheet_token, ops,
        label=f"set-cell {sheet_name}!{range_str}={value!r}"
              + (f' +style {sorted(style_applied.keys())}' if style_applied else ''),
    )
    _emit({
        'ok': True, 'new_rev': new_rev, 'sheet': sheet_name,
        'range': range_str, 'value': value, 'style': style_applied,
    })


# ---------------------------------------------------------------------------
# set-formula — write a formula to a single cell
# ---------------------------------------------------------------------------

def cmd_sheet_set_formula(cookies, url: str, cell: str, formula: str, opts: dict = None):
    opts = opts or {}
    spreadsheet_token, sheet_id, sheet_name, _, _ = _resolve(cookies, url, opts.get('sheetName'))
    rng = parse_range(cell)
    if rng['rowCount'] != 1 or rng['colCount'] != 1:
        raise ValueError('set-formula requires single-cell range')
    if not formula.startswith('='):
        formula = '=' + formula
    op = sheet_ot.op_write_cell(sheet_id, rng['row'], rng['col'], formula=formula)
    new_rev = _commit(cookies, spreadsheet_token, op, label=f"set-formula {sheet_name}!{cell}={formula}")
    _emit({'ok': True, 'new_rev': new_rev, 'sheet': sheet_name, 'cell': cell, 'formula': formula})


# ---------------------------------------------------------------------------
# bold — toggle bold on a range
# ---------------------------------------------------------------------------

def cmd_sheet_set_style(cookies, url: str, range_str: str, opts: dict = None):
    """Apply a uniform style to a rectangular range without touching values.

    `range_str` accepts A1 (single cell) or A1:B3 (rectangle). Whole-row
    (`1:1`) / whole-column (`A:A`) forms are NOT supported yet — parse_range
    can't handle them, and the server-side enum for those targets has not
    been reverse-engineered.
    """
    opts = opts or {}
    spreadsheet_token, sheet_id, sheet_name, _, _ = _resolve(cookies, url, opts.get('sheetName'))
    rng = parse_range(range_str)

    style_json = opts.get('style')
    if not style_json:
        raise ValueError('set-style requires --style JSON')
    try:
        ai_style = json.loads(style_json) if isinstance(style_json, str) else style_json
    except json.JSONDecodeError as e:
        raise ValueError(f'invalid --style JSON: {e}')
    style_kwargs = _ai_style_to_kwargs(ai_style)
    if not style_kwargs:
        raise ValueError('--style is empty after translation; nothing to apply')

    op = sheet_ot.op_style_range(
        sheet_id, rng['row'], rng['col'], rng['rowCount'], rng['colCount'],
        **style_kwargs,
    )
    new_rev = _commit(
        cookies, spreadsheet_token, op,
        label=f"set-style {sheet_name}!{range_str} style={sorted(ai_style.keys())}",
    )
    _emit({
        'ok': True, 'new_rev': new_rev, 'sheet': sheet_name,
        'range': range_str, 'style': ai_style,
    })


# ---------------------------------------------------------------------------
# set-range — bulk write a 2D array of values in a single OT commit
# ---------------------------------------------------------------------------

# AI-friendly enum → server enum
_H_ALIGN_MAP = {'left': 0, 'center': 1, 'right': 2}
_V_ALIGN_MAP = {'top': 0, 'middle': 1, 'bottom': 2}


def _ai_style_to_kwargs(s: dict) -> dict:
    """Translate the AI-friendly style schema (bold / italic / color / ...) into
    op_style_range kwargs. Unknown keys raise ValueError to fail loudly on typos.
    """
    if not isinstance(s, dict):
        raise ValueError(f'--style must be a JSON object, got: {type(s).__name__}')
    known = {
        'bold', 'italic', 'underline', 'color', 'bgColor',
        'fontSize', 'fontFamily', 'align', 'vAlign', 'numberFormat',
    }
    bad = set(s.keys()) - known
    if bad:
        raise ValueError(f'unknown style keys: {sorted(bad)}; allowed: {sorted(known)}')
    out = {}
    if 'bold' in s: out['bold'] = bool(s['bold'])
    if 'italic' in s: out['italic'] = bool(s['italic'])
    if 'underline' in s: out['underline'] = bool(s['underline'])
    if 'color' in s: out['color'] = s['color']
    if 'bgColor' in s: out['bg_color'] = s['bgColor']
    if 'fontSize' in s: out['font_size'] = s['fontSize']
    if 'fontFamily' in s: out['font_family'] = s['fontFamily']
    if 'align' in s:
        v = s['align']
        if isinstance(v, str):
            if v not in _H_ALIGN_MAP:
                raise ValueError(f'align must be left/center/right or 0-2, got: {v!r}')
            out['h_align'] = _H_ALIGN_MAP[v]
        else:
            out['h_align'] = int(v)
    if 'vAlign' in s:
        v = s['vAlign']
        if isinstance(v, str):
            if v not in _V_ALIGN_MAP:
                raise ValueError(f'vAlign must be top/middle/bottom or 0-2, got: {v!r}')
            out['v_align'] = _V_ALIGN_MAP[v]
        else:
            out['v_align'] = int(v)
    if 'numberFormat' in s: out['number_format'] = s['numberFormat']
    return out


def cmd_sheet_set_range(
    cookies, url: str, start_cell: str, values_json: str, opts: dict = None,
):
    """Write a 2D array of values starting at start_cell, optionally with a
    uniform style applied over the resulting range. Sends N WRITE_CELL ops
    plus at most 1 STYLE_RANGE op in a SINGLE OT commit — one round-trip
    instead of N.
    """
    opts = opts or {}
    spreadsheet_token, sheet_id, sheet_name, _, _ = _resolve(cookies, url, opts.get('sheetName'))

    # Parse and validate values
    try:
        values = json.loads(values_json)
    except json.JSONDecodeError as e:
        raise ValueError(f'invalid values JSON: {e}')
    if not isinstance(values, list) or not values:
        raise ValueError('values must be a non-empty 2D array')
    if not all(isinstance(row, list) for row in values):
        raise ValueError('values must be a 2D array (list of lists)')
    row_count = len(values)
    col_count = max((len(row) for row in values), default=0)
    if col_count == 0:
        raise ValueError('values has no columns')

    # Parse start cell (must be single cell, not a range like A1:B2)
    rng_start = parse_range(start_cell)
    if rng_start['rowCount'] != 1 or rng_start['colCount'] != 1:
        raise ValueError(
            f'start_cell must be a single cell (e.g. A1), got range: {start_cell!r}'
        )
    start_row = rng_start['row']
    start_col = rng_start['col']

    # Build WRITE_CELL ops — skip None / empty cells so callers can write
    # ragged rectangles without touching the unset cells.
    ops = []
    cells_written = 0
    for ri, row_values in enumerate(values):
        for ci, value in enumerate(row_values):
            if value is None or value == '':
                continue
            ops.append(sheet_ot.op_write_cell(
                sheet_id, start_row + ri, start_col + ci, value=value,
                raw_string=bool(opts.get('raw_string')),
            ))
            cells_written += 1

    if not ops and not opts.get('style'):
        raise ValueError('nothing to do: all cells are empty and no --style given')

    # Optional uniform style over the rectangle
    style_applied = None
    style_json = opts.get('style')
    if style_json:
        try:
            ai_style = json.loads(style_json) if isinstance(style_json, str) else style_json
        except json.JSONDecodeError as e:
            raise ValueError(f'invalid --style JSON: {e}')
        style_kwargs = _ai_style_to_kwargs(ai_style)
        ops.append(sheet_ot.op_style_range(
            sheet_id, start_row, start_col, row_count, col_count, **style_kwargs,
        ))
        style_applied = ai_style

    label = f'set-range {sheet_name}!{start_cell}+{row_count}x{col_count} ({cells_written} cells'
    if style_applied:
        label += f', +style {sorted(style_applied.keys())}'
    label += ')'
    new_rev = _commit(cookies, spreadsheet_token, ops, label=label)
    _emit({
        'ok': True, 'new_rev': new_rev, 'sheet': sheet_name,
        'startCell': start_cell, 'rows': row_count, 'cols': col_count,
        'cellsWritten': cells_written, 'style': style_applied,
    })


# ---------------------------------------------------------------------------
# insert-row / delete-row / insert-col / delete-col
# ---------------------------------------------------------------------------

def cmd_sheet_insert_row(cookies, url: str, opts: dict = None):
    opts = opts or {}
    spreadsheet_token, sheet_id, sheet_name, _, _ = _resolve(cookies, url, opts.get('sheetName'))
    at = int(opts['at']) - 1  # 1-based UI → 0-based wire
    count = int(opts.get('count') or 1)
    if at < 0 or count < 1:
        raise ValueError('--at must be >= 1 and --count must be >= 1')
    op = sheet_ot.op_insert_row(sheet_id, at, count)
    new_rev = _commit(cookies, spreadsheet_token, op, label=f"insert-row {sheet_name}@{at + 1} x{count}")
    _emit({'ok': True, 'new_rev': new_rev, 'sheet': sheet_name, 'at': at + 1, 'count': count})


def cmd_sheet_delete_row(cookies, url: str, opts: dict = None):
    opts = opts or {}
    spreadsheet_token, sheet_id, sheet_name, _, _ = _resolve(cookies, url, opts.get('sheetName'))
    at = int(opts['at']) - 1
    count = int(opts.get('count') or 1)
    if at < 0 or count < 1:
        raise ValueError('--at must be >= 1 and --count must be >= 1')
    op = sheet_ot.op_delete_row(sheet_id, at, count)
    new_rev = _commit(cookies, spreadsheet_token, op, label=f"delete-row {sheet_name}@{at + 1} x{count}")
    _emit({'ok': True, 'new_rev': new_rev, 'sheet': sheet_name, 'at': at + 1, 'count': count})


def cmd_sheet_insert_col(cookies, url: str, opts: dict = None):
    opts = opts or {}
    spreadsheet_token, sheet_id, sheet_name, _, _ = _resolve(cookies, url, opts.get('sheetName'))
    at = parse_col_letter(opts['at'])
    count = int(opts.get('count') or 1)
    op = sheet_ot.op_insert_column(sheet_id, at, count)
    new_rev = _commit(cookies, spreadsheet_token, op, label=f"insert-col {sheet_name}@col {at} x{count}")
    _emit({'ok': True, 'new_rev': new_rev, 'sheet': sheet_name, 'col': opts['at'], 'count': count})


def cmd_sheet_delete_col(cookies, url: str, opts: dict = None):
    opts = opts or {}
    spreadsheet_token, sheet_id, sheet_name, _, _ = _resolve(cookies, url, opts.get('sheetName'))
    at = parse_col_letter(opts['at'])
    count = int(opts.get('count') or 1)
    op = sheet_ot.op_delete_column(sheet_id, at, count)
    new_rev = _commit(cookies, spreadsheet_token, op, label=f"delete-col {sheet_name}@col {at} x{count}")
    _emit({'ok': True, 'new_rev': new_rev, 'sheet': sheet_name, 'col': opts['at'], 'count': count})


# ---------------------------------------------------------------------------
# add-tab / delete-tab / rename-tab
# ---------------------------------------------------------------------------

def cmd_sheet_add_tab(cookies, url: str, opts: dict = None):
    opts = opts or {}
    name = opts.get('name')
    if not name:
        raise ValueError('--name is required')
    info = resolve_sheet_token(cookies, url)
    spreadsheet_token = info['spreadsheetToken']
    sheets = fetch_sheet_list(cookies, spreadsheet_token)
    index = int(opts['at']) if opts.get('at') is not None else len(sheets)
    new_id = sheet_ot.new_sheet_id()
    op = sheet_ot.op_create_sheet(new_id, name, index)
    new_rev = _commit(cookies, spreadsheet_token, op, label=f"add-tab {name!r}@{index}")
    _emit({'ok': True, 'new_rev': new_rev, 'newSheetId': new_id, 'name': name, 'index': index})


def cmd_sheet_delete_tab(cookies, url: str, opts: dict = None):
    opts = opts or {}
    name = opts.get('name')
    if not name:
        raise ValueError('--name is required')
    spreadsheet_token, sheet_id, sheet_name, sheet_index, _ = _resolve(cookies, url, name)
    op = sheet_ot.op_delete_sheet(sheet_id, sheet_name, sheet_index)
    new_rev = _commit(cookies, spreadsheet_token, op, label=f"delete-tab {sheet_name!r}@{sheet_index}")
    _emit({'ok': True, 'new_rev': new_rev, 'name': sheet_name, 'index': sheet_index})


def cmd_sheet_rename_tab(cookies, url: str, opts: dict = None):
    opts = opts or {}
    old = opts.get('fromName')
    new = opts.get('toName')
    if not old or not new:
        raise ValueError('--from and --to are required')
    spreadsheet_token, sheet_id, _, _, _ = _resolve(cookies, url, old)
    op = sheet_ot.op_rename_sheet(sheet_id, old, new)
    new_rev = _commit(cookies, spreadsheet_token, op, label=f"rename-tab {old!r} → {new!r}")
    _emit({'ok': True, 'new_rev': new_rev, 'from': old, 'to': new})


# ---------------------------------------------------------------------------
# state-clear — wipe local base_rev cache
# ---------------------------------------------------------------------------

def cmd_sheet_state_clear(opts: dict = None):
    opts = opts or {}
    token = opts.get('token')
    if token:
        sheet_state.clear_rev(token)
        _emit({'ok': True, 'cleared': token})
    else:
        existing = sheet_state.list_revs()
        sheet_state.clear_all()
        _emit({'ok': True, 'clearedAll': True, 'previous': existing})
