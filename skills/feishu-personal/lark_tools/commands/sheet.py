"""
commands/sheet.py - Spreadsheet read commands (mirrors lib/commands/sheet.js)
"""

import base64
import io
import json
import os
import re
import struct
import sys
import zipfile
import zlib
import xml.etree.ElementTree as ET

from ..config import DOC_HOST
from ..http_utils import http_get, http_post_json, cookies_to_dict
from ..proto import decode_varint
from ..sheet_decoder import decode_proto_raw as _decode_proto_raw  # noqa: F401


# ---------------------------------------------------------------------------
# _post_sheet_api
# ---------------------------------------------------------------------------

def _post_sheet_api(cookies, body: dict):
    cookie_str = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
    res = http_post_json(
        f'https://{DOC_HOST}/space/api/v3/sheet/client_vars',
        body,
        {'Cookie': cookie_str, 'Referer': f'https://{DOC_HOST}/'},
    )
    return res.get('body') if isinstance(res, dict) else None


# ---------------------------------------------------------------------------
# _extract_sheet_names  (gzipTopSnapshot protobuf → {sheetId: name})
# ---------------------------------------------------------------------------

_SHEET_ID_RE = re.compile(r'^[0-9a-zA-Z]{6}$')
_SHEET_NAME_RE = re.compile(r'^[\x20-\x7E\u4e00-\u9fff]{1,100}$')


def _walk_pb_fields(buf: bytes):
    """Yield (field_number, wire_type, value) for every protobuf field in `buf`.
    For wt=2 (length-delimited) value is raw bytes; caller may recurse.
    Stops silently on malformed input.
    """
    pos = 0
    try:
        while pos < len(buf):
            tag, pos = decode_varint(buf, pos)
            fn = tag >> 3
            wt = tag & 7
            if wt == 0:
                v, pos = decode_varint(buf, pos)
                yield fn, wt, v
            elif wt == 2:
                length, pos = decode_varint(buf, pos)
                v = buf[pos:pos + length]
                pos += length
                yield fn, wt, v
            elif wt == 1:
                pos += 8
            elif wt == 5:
                pos += 4
            else:
                return
    except Exception:
        return


def _scan_sheet_pairs(gzip_top_snapshot: str):
    """Deep-walk gzipTopSnapshot protobuf, yield (sheet_id, sheet_name) in tab order.

    Sheet info lives ~4 levels deep (top.f1 → f2 → f3 → f1 → leaf), and the
    earlier shallow walk only inspected top.f1 children — silently returned
    nothing on every real spreadsheet, which made `fetch_sheet_list` fall back
    to synthetic 'Sheet{i+1}' names. This recursive scan finds the leaf message
    by signature: f1 is a 6-char alphanumeric sheetId AND f3 is a printable
    string name.

    Note: sheetId regex relaxed from ^[0-9a-f]{6}$ to ^[0-9a-zA-Z]{6}$ because
    real sheet IDs like `sdM9B5` may contain non-hex letters.
    """
    seen = set()

    def visit(buf: bytes, depth: int = 0):
        if depth > 8 or not buf:
            return
        local_id = None
        local_name = None
        for fn, wt, val in _walk_pb_fields(buf):
            if wt != 2:
                continue
            try:
                s = val.decode('utf-8')
                printable = bool(s) and bool(_SHEET_NAME_RE.match(s))
            except UnicodeDecodeError:
                s = None
                printable = False
            if printable:
                if fn == 1 and _SHEET_ID_RE.match(s):
                    local_id = s
                elif fn == 3:
                    local_name = s
            # Always recurse into nested message — string-match is best-effort
            if not printable or not (_SHEET_ID_RE.match(s or '') or len(s or '') < 4):
                yield from visit(val, depth + 1)
        if local_id and local_name and local_id not in seen:
            seen.add(local_id)
            yield local_id, local_name

    try:
        buf = zlib.decompress(base64.b64decode(gzip_top_snapshot), 47)
    except Exception:
        return
    yield from visit(buf)


def _extract_sheet_names(gzip_top_snapshot: str) -> dict:
    """Backward-compatible wrapper: returns {sheetId: name}, no order guarantee.
    Use _extract_sheet_order if tab order matters.
    """
    return {sid: name for sid, name in _scan_sheet_pairs(gzip_top_snapshot)}


def _extract_sheet_order(gzip_top_snapshot: str) -> list:
    """Returns [(sheetId, name)] in protobuf-walk order, which matches tab order on screen."""
    return list(_scan_sheet_pairs(gzip_top_snapshot))


# ---------------------------------------------------------------------------
# fetch_sheet_list
# ---------------------------------------------------------------------------

def fetch_sheet_list(cookies, spreadsheet_token: str) -> list:
    """Return [{index, sheetId, name, rowCount, colCount, hidden}] in tab order.

    Source of truth for the sheet list is `gzipTopSnapshot` — present from
    creation, lists every tab including the initial Sheet1 of a fresh
    spreadsheet that has no cells yet. `gzipBlockMeta` is per-cell-block info
    and is only populated after the first cell write, so iterating it as the
    primary source (the previous implementation) silently returned 0 sheets on
    every fresh spreadsheet and forced names to 'Sheet{i+1}' fallbacks.
    """
    res = _post_sheet_api(cookies, {
        'memberId': 0, 'schemaVersion': 9, 'openType': 1,
        'token': spreadsheet_token, 'clientVersion': 'v0.0.1',
    })
    if not res or res.get('code') != 0 or not res.get('data'):
        raise RuntimeError(f"Sheet list API failed: code={res and res.get('code')} msg={res and res.get('msg')}")

    snapshot = res['data'].get('snapshot') or {}
    block_meta = {}
    if snapshot.get('gzipBlockMeta'):
        try:
            block_meta = json.loads(zlib.decompress(base64.b64decode(snapshot['gzipBlockMeta']), 47).decode('utf-8'))
        except Exception:
            pass

    sheets_in_top = _extract_sheet_order(snapshot['gzipTopSnapshot']) if snapshot.get('gzipTopSnapshot') else []

    # Primary path: tabs from topSnapshot in tab order (real names, works on fresh sheets)
    if sheets_in_top:
        sheets = []
        for i, (sheet_id, name) in enumerate(sheets_in_top):
            range_info = None
            meta = (block_meta or {}).get(sheet_id) or {}
            cell_block_metas = meta.get('cellBlockMetas') or []
            if cell_block_metas and cell_block_metas[0].get('range'):
                range_info = cell_block_metas[0]['range']
            sheets.append({
                'index': i,
                'sheetId': sheet_id,
                'name': name,
                'rowCount': (range_info['rowEnd'] + 1) if range_info else 0,
                'colCount': (range_info['colEnd'] + 1) if range_info else 0,
                'hidden': False,
            })
        return sheets

    # Fallback: legacy block_meta-driven path (defensive — should not normally trigger
    # since topSnapshot is always present)
    sheet_names = _extract_sheet_names(snapshot['gzipTopSnapshot']) if snapshot.get('gzipTopSnapshot') else {}
    sheets = []
    for i, (sheet_id, meta) in enumerate(block_meta.items()):
        range_info = None
        cell_block_metas = meta.get('cellBlockMetas') or []
        if cell_block_metas and cell_block_metas[0].get('range'):
            range_info = cell_block_metas[0]['range']
        sheets.append({
            'index': i,
            'sheetId': sheet_id,
            'name': sheet_names.get(sheet_id) or f'Sheet{i + 1}',
            'rowCount': (range_info['rowEnd'] + 1) if range_info else 0,
            'colCount': (range_info['colEnd'] + 1) if range_info else 0,
            'hidden': False,
        })
    return sheets


# ---------------------------------------------------------------------------
# _parse_cell_block  (gzipped protobuf → 2D list of strings)
# ---------------------------------------------------------------------------

def _parse_cell_block(cell_buf: bytes) -> list:
    top = _decode_proto_raw(cell_buf)
    inner = _decode_proto_raw(bytes(top.get('f1', b'')))
    f2_data = _decode_proto_raw(bytes(inner.get('f2', b'')))
    f12 = _decode_proto_raw(bytes(f2_data.get('f12', b'')))
    meta = _decode_proto_raw(bytes(f12.get('f1', b'')))

    ROWS = meta.get('f3', 0)
    COLS = meta.get('f4', 0)
    if isinstance(ROWS, bytes):
        ROWS, _ = decode_varint(ROWS, 0)
    if isinstance(COLS, bytes):
        COLS, _ = decode_varint(COLS, 0)
    if not ROWS or not COLS:
        raise ValueError('Empty sheet')

    # Parse value pool from f12.f2
    doubles_pool, text_entries, rich_entries = [], [], []
    f2_raw = bytes(f12.get('f2', b''))
    pos = 0
    while pos < len(f2_raw):
        try:
            tag, pos = decode_varint(f2_raw, pos)
            fn = tag >> 3
            wt = tag & 7
            if wt == 2:
                length, pos = decode_varint(f2_raw, pos)
                chunk = f2_raw[pos:pos + length]
                pos += length
                if fn == 1:
                    for i in range(0, len(chunk) - 7, 8):
                        doubles_pool.append(struct.unpack_from('<d', chunk, i)[0])
                elif fn == 2:
                    text_entries.append(chunk.decode('utf-8', errors='replace'))
                elif fn == 3:
                    rr = _decode_proto_raw(chunk)
                    ti = rr.get('f1', -1)
                    if isinstance(ti, bytes):
                        ti, _ = decode_varint(bytes(ti), 0)
                    rich_entries.append({'textIdx': ti})
                elif fn == 5:
                    sub = _decode_proto_raw(chunk)
                    idx = sub.get('f2', 0)
                    if isinstance(idx, bytes):
                        idx, _ = decode_varint(bytes(idx), 0)
                    doubles_pool.append(idx)  # numEntries not needed separately
            elif wt == 0:
                _, pos = decode_varint(f2_raw, pos)
            else:
                break
        except Exception:
            break

    # Parse f12.f6: index_map + cell_meta
    f6_raw = bytes(f12.get('f6', b''))
    index_map, cell_meta = None, []
    pos = 0
    while pos < len(f6_raw):
        try:
            tag, pos = decode_varint(f6_raw, pos)
            fn = tag >> 3
            wt = tag & 7
            if wt == 2:
                length, pos = decode_varint(f6_raw, pos)
                chunk = f6_raw[pos:pos + length]
                pos += length
                if fn == 1:
                    index_map = chunk
                elif fn == 2:
                    entry = _decode_proto_raw(chunk)
                    cell_meta.append({k: (v if not isinstance(v, bytes) else int.from_bytes(v[:8], 'little')) for k, v in entry.items()})
            elif wt == 0:
                _, pos = decode_varint(f6_raw, pos)
            else:
                break
        except Exception:
            break

    if not index_map:
        raise ValueError('Empty index map')
    # Sparse cell blocks: index_map shorter than ROWS*COLS means trailing
    # positions are empty (idx=0). Longer is defensive (truncate). Without
    # this, a freshly-OT-modified sheet that only wrote a handful of cells
    # raised "Index map size mismatch" because the server returns index_map
    # bytes only up to the last non-empty cell.
    expected = ROWS * COLS
    if len(index_map) < expected:
        index_map = index_map + b'\x00' * (expected - len(index_map))
    elif len(index_map) > expected:
        index_map = index_map[:expected]

    grid = []
    for r in range(ROWS):
        row = []
        for c in range(COLS):
            idx = index_map[r * COLS + c]
            if idx == 0 or idx >= len(cell_meta):
                row.append('')
                continue
            cm = cell_meta[idx]
            f1 = cm.get('f1', 0)
            f2 = cm.get('f2')
            f3 = cm.get('f3')
            if f1 == 3 and f2 is not None:
                row.append(text_entries[f2] if f2 < len(text_entries) else '')
            elif f1 == 6 and f2 is not None:
                rich = rich_entries[f2] if f2 < len(rich_entries) else None
                ti = rich['textIdx'] if rich else -1
                row.append(text_entries[ti] if rich and 0 <= ti < len(text_entries) else '')
            elif f1 == 2 and f2 is not None:
                v = doubles_pool[f2] if f2 < len(doubles_pool) else None
                row.append(str(v) if v is not None else '')
            elif f1 == 4:
                # Formula cell. f2/f3 are dependency pool indexes (e.g. for
                # `=A1+B1` they point to A1's and B1's slots in doubles_pool),
                # NOT the evaluated result. Server only ships the eval value
                # via the Pandora WS push channel — the REST cell snapshot
                # never includes it (verified 2026-05-18: even after the cache
                # warms up, 30.0 for `=A1+B1` never appears in the bytes).
                # Use `<formula>` placeholder so callers can distinguish formula
                # cells from truly empty cells. To get evaluated values, export
                # via `sheet download` (xlsx preserves the formula, Excel/
                # openpyxl recalculates on open).
                row.append('<formula>')
            elif f1 == 7:
                # Attachment / image cell. f2 is the index into f12.f3 image
                # resource list (1-based). The image data is NOT in the cell
                # meta — use fetch_sheet_images() to extract full image info.
                row.append('<image>')
            else:
                row.append('')
        grid.append(row)

    while len(grid) > 1 and all(c == '' for c in grid[-1]):
        grid.pop()
    return grid


# ---------------------------------------------------------------------------
# _get_raw_cell_block  — raw protobuf for image extraction
# ---------------------------------------------------------------------------

def _get_raw_cell_block(cookies, spreadsheet_token: str, sheet_id: str) -> bytes:
    """Fetch and decompress the raw cell block bytes for a sheet.

    Returns the decompressed protobuf bytes, or raises RuntimeError.
    This is the shared low-level fetch used by both _parse_cell_block
    (for text grid) and _parse_sheet_image_entries (for image extraction).
    """
    res = _post_sheet_api(cookies, {
        'memberId': 0, 'schemaVersion': 9, 'openType': 1,
        'token': spreadsheet_token, 'sheetRange': {'sheetId': sheet_id},
        'clientVersion': 'v0.0.1',
    })
    if not res or res.get('code') != 0 or not res.get('data') or not res['data'].get('snapshot'):
        raise RuntimeError(f"Sheet cell API failed: code={res and res.get('code')} msg={res and res.get('msg')}")
    snapshot = res['data']['snapshot']
    block_keys = list((snapshot.get('blocks') or {}).keys())
    if not block_keys:
        raise RuntimeError('No cell blocks in response')
    return zlib.decompress(base64.b64decode(snapshot['blocks'][block_keys[0]]), 47)


# ---------------------------------------------------------------------------
# Column letter helpers
# ---------------------------------------------------------------------------

def _col_index_to_letter(idx: int) -> str:
    """Convert 0-based column index to Excel-style letter(s). E.g. 0→A, 25→Z, 26→AA."""
    result = ''
    while idx >= 0:
        result = chr(ord('A') + idx % 26) + result
        idx = idx // 26 - 1
    return result


def _col_letter_to_index(letter: str) -> int:
    """Convert Excel-style column letter(s) to 0-based index. E.g. A→0, Z→25, AA→26."""
    idx = 0
    for ch in letter.upper():
        idx = idx * 26 + (ord(ch) - ord('A') + 1)
    return idx - 1


def _parse_cell_range(cell_ref: str):
    """Parse 'A1', 'E3' etc. into (0-based row, 0-based col)."""
    m = re.match(r'^([A-Za-z]+)(\d+)$', cell_ref.strip().upper())
    if not m:
        raise ValueError(f"Invalid cell reference: {cell_ref!r}. Expected format like 'A1', 'E3', 'AA12'.")
    col_letter, row_str = m.groups()
    return int(row_str) - 1, _col_letter_to_index(col_letter)


# ---------------------------------------------------------------------------
# _parse_sheet_image_entries  — extract image resources from a cell block
# ---------------------------------------------------------------------------

def _parse_sheet_image_entries(cell_buf: bytes) -> dict:
    """Extract image/attachment entries from a raw sheet cell block.

    Returns a dict with:
      - images: list of dicts, each with:
          index (int): 0-based index in the resource list
          token (str): image resource token used for download
          width (float or None): image width in pixels
          height (float or None): image height in pixels
          raw_width (float or None): raw width value from protobuf
          raw_height (float or None): raw height value from protobuf
      - cell_map: dict mapping 0-based image index → list of cell addresses
        (e.g. {0: ['B3'], 1: ['E3'], 2: ['F5']})
      - dims: {'rows': int, 'cols': int} — sheet dimensions

    Image cells have cell-meta type f1=7. The f2 value on such cells is the
    0-based index into the f12.f3 resource list.

    The image download URL for each image token is constructed as:
      https://internal-api-drive-stream.feishu.cn/space/api/box/stream/
      download/v2/cover/{token}/?height=4096&mount_node_token={spreadsheet_token}
      &mount_point=sheet_image&policy=equal&width=4096
    """
    top = _decode_proto_raw(cell_buf)
    inner = _decode_proto_raw(bytes(top.get('f1', b'')))
    f2_data = _decode_proto_raw(bytes(inner.get('f2', b'')))
    f12 = _decode_proto_raw(bytes(f2_data.get('f12', b'')))
    meta = _decode_proto_raw(bytes(f12.get('f1', b'')))

    ROWS = meta.get('f3', 0)
    COLS = meta.get('f4', 0)
    if isinstance(ROWS, bytes):
        ROWS, _ = decode_varint(ROWS, 0)
    if isinstance(COLS, bytes):
        COLS, _ = decode_varint(COLS, 0)

    # ── Parse image resource entries from f12.f3 ──
    images = []
    if 'f3' in f12:
        f3_raw = bytes(f12['f3'])
        f3 = _decode_proto_raw(f3_raw)
        image_entries = f3.get('f1', [])
        if not isinstance(image_entries, list):
            image_entries = [image_entries]

        for i, entry_bytes in enumerate(image_entries):
            entry = _decode_proto_raw(bytes(entry_bytes))
            token = None
            width = None
            height = None
            raw_width = None
            raw_height = None

            # f1: image token (used for download)
            if 'f1' in entry and isinstance(entry['f1'], bytes):
                token = entry['f1'].decode('utf-8', errors='replace')

            # f2: dimensions as protobuf {f1: width, f2: height} (doubles)
            if 'f2' in entry and isinstance(entry['f2'], bytes):
                try:
                    dims = _decode_proto_raw(bytes(entry['f2']))
                    for dk, dv in dims.items():
                        if dk == 'f1':
                            raw_width = dv
                            width = round(dv) if isinstance(dv, float) else dv
                        elif dk == 'f2':
                            raw_height = dv
                            height = round(dv) if isinstance(dv, float) else dv
                except Exception:
                    pass

            images.append({
                'index': i,
                'token': token,
                'width': width,
                'height': height,
                'raw_width': raw_width,
                'raw_height': raw_height,
            })

    # ── Build cell_map: scan f12.f6 for cells with type f1=7 ──
    cell_map = {}
    f6_raw = bytes(f12.get('f6', b''))
    index_map = None
    cell_meta_list = []
    pos = 0
    while pos < len(f6_raw):
        try:
            tag, pos = decode_varint(f6_raw, pos)
            fn = tag >> 3
            wt = tag & 7
            if wt == 2:
                length, pos = decode_varint(f6_raw, pos)
                chunk = f6_raw[pos:pos + length]
                pos += length
                if fn == 1:
                    index_map = chunk
                elif fn == 2:
                    entry = _decode_proto_raw(chunk)
                    cell_meta_list.append(entry)
            elif wt == 0:
                _, pos = decode_varint(f6_raw, pos)
            else:
                break
        except Exception:
            break

    if index_map and ROWS and COLS:
        expected = ROWS * COLS
        if len(index_map) < expected:
            index_map = index_map + b'\x00' * (expected - len(index_map))
        elif len(index_map) > expected:
            index_map = index_map[:expected]

        for r in range(ROWS):
            for c in range(COLS):
                idx = index_map[r * COLS + c]
                if idx == 0 or idx >= len(cell_meta_list):
                    continue
                cm = cell_meta_list[idx]
                f1_val = cm.get('f1', 0)
                if isinstance(f1_val, bytes):
                    f1_val = int.from_bytes(f1_val[:8], 'little')
                # f1=7 is the attachment/image cell type
                if f1_val == 7:
                    f2_val = cm.get('f2', 0)
                    if isinstance(f2_val, bytes):
                        f2_val = int.from_bytes(f2_val[:8], 'little')
                    # f2 is 0-based index into f12.f3 resource list.
                    # f2=0 is valid (first image), so don't guard with >0.
                    cell_addr = f'{_col_index_to_letter(c)}{r + 1}'
                    if f2_val not in cell_map:
                        cell_map[f2_val] = []
                    cell_map[f2_val].append(cell_addr)

    return {
        'images': images,
        'cell_map': cell_map,
        'dims': {'rows': ROWS, 'cols': COLS},
    }


# ---------------------------------------------------------------------------
# fetch_sheet_cells
# ---------------------------------------------------------------------------

def fetch_sheet_cells(cookies, spreadsheet_token: str, sheet_id: str) -> list:
    cell_buf = _get_raw_cell_block(cookies, spreadsheet_token, sheet_id)
    return _parse_cell_block(cell_buf)


# ---------------------------------------------------------------------------
# fetch_sheet_images
# ---------------------------------------------------------------------------

def fetch_sheet_images(cookies, spreadsheet_token: str, sheet_id: str) -> dict:
    """Extract all image/attachment entries from a sheet.

    Returns dict with:
      - spreadsheetToken (str)
      - sheetId (str)
      - dims: {'rows': int, 'cols': int}
      - images: list of image entries, each with:
          index (int), token (str), width (float|None), height (float|None)
      - cell_map: {image_index: [cell_address, ...]}

    To download a specific image, use:
      lark_tools.commands.img.download_sheet_image(cookies, token, spreadsheet_token, output_path)
    or the cover URL pattern:
      https://internal-api-drive-stream.feishu.cn/space/api/box/stream/
      download/v2/cover/{token}/?height=4096&mount_node_token={spreadsheet_token}
      &mount_point=sheet_image&policy=equal&width=4096
    """
    cell_buf = _get_raw_cell_block(cookies, spreadsheet_token, sheet_id)
    result = _parse_sheet_image_entries(cell_buf)
    result['spreadsheetToken'] = spreadsheet_token
    result['sheetId'] = sheet_id
    return result


# ---------------------------------------------------------------------------
# resolve_sheet_token
# ---------------------------------------------------------------------------

def resolve_sheet_token(cookies, input_str: str) -> dict:
    token = input_str
    is_wiki = False
    if '/' in token:
        m = re.search(r'/(wiki|sheets?|spreadsheets?)/([A-Za-z0-9]+)', token)
        if m:
            is_wiki = m.group(1) == 'wiki'
            token = m.group(2)

    if is_wiki:
        node_res = http_get(cookies, DOC_HOST, f'/space/api/wiki/v2/tree/get_node/?wiki_token={token}&expand_shortcut=true')
        node_data = (node_res.get('data') or {}).get('data') or {}
        if (node_res.get('data') or {}).get('code') == 0 and node_data.get('obj_token'):
            obj_type = node_data.get('obj_type', 'sheet')
            print(f"[sheet] Wiki resolved: obj_type={obj_type} obj_token={node_data['obj_token']}", file=sys.stderr)
            return {'spreadsheetToken': node_data['obj_token'], 'objType': obj_type}
        raise RuntimeError(f'Wiki resolution failed for token: {token}')
    return {'spreadsheetToken': token, 'objType': 'sheet'}


# ---------------------------------------------------------------------------
# grid_to_markdown
# ---------------------------------------------------------------------------

def grid_to_markdown(grid: list, sheet_name: str = None) -> str:
    def esc(s):
        return s.replace('\n', ' ').replace('|', '\\|').strip()

    lines = []
    if sheet_name:
        lines.append(f'## {sheet_name}\n')
    if not grid:
        return '\n'.join(lines)
    lines.append('| ' + ' | '.join(esc(c) for c in grid[0]) + ' |')
    lines.append('| ' + ' | '.join('---' for _ in grid[0]) + ' |')
    for row in grid[1:]:
        lines.append('| ' + ' | '.join(esc(c) for c in row) + ' |')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# cmd_sheet_tables / cmd_sheet_read / cmd_sheet_download / cmd_sheet_images
# ---------------------------------------------------------------------------

def cmd_sheet_tables(cookies, input_str: str):
    info = resolve_sheet_token(cookies, input_str)
    spreadsheet_token = info['spreadsheetToken']
    print(f'[sheet] Fetching sheet list for {spreadsheet_token}...', file=sys.stderr)
    sheets = fetch_sheet_list(cookies, spreadsheet_token)
    print(json.dumps({'spreadsheetToken': spreadsheet_token, 'sheets': sheets}, indent=2))


def _select_sheets(sheets, opts):
    sheet_name = opts.get('sheetName')
    if sheet_name:
        by_name = [s for s in sheets if s['name'] == sheet_name]
        if not by_name:
            try:
                return [sheets[int(sheet_name)]]
            except (ValueError, IndexError):
                pass
            available = ', '.join(s['name'] for s in sheets)
            raise RuntimeError(f'Sheet not found: "{sheet_name}". Available: {available}')
        return by_name
    if opts.get('all'):
        return [s for s in sheets if not s.get('hidden')]
    return [sheets[0]]


def _xlsx_sheet_names(xlsx_bytes: bytes) -> list:
    """Return workbook sheet names from an xlsx package without rewriting it."""
    try:
        with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zf:
            workbook_xml = zf.read('xl/workbook.xml')
        root = ET.fromstring(workbook_xml)
    except Exception:
        return []

    ns = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    return [
        node.attrib.get('name', '')
        for node in root.findall('main:sheets/main:sheet', ns)
        if node.attrib.get('name')
    ]


def cmd_sheet_read(cookies, input_str: str, opts: dict = None):
    opts = opts or {}
    info = resolve_sheet_token(cookies, input_str)
    spreadsheet_token = info['spreadsheetToken']
    print(f'[sheet] Fetching sheet list for {spreadsheet_token}...', file=sys.stderr)
    sheets = fetch_sheet_list(cookies, spreadsheet_token)
    if not sheets:
        raise RuntimeError('No sheets found in spreadsheet')

    selected = _select_sheets(sheets, opts)
    md_parts = []
    for sheet in selected:
        print(f'[sheet] Reading "{sheet["name"]}" ({sheet["sheetId"]})...', file=sys.stderr)
        try:
            grid = fetch_sheet_cells(cookies, spreadsheet_token, sheet['sheetId'])
            label = sheet['name'] if len(selected) > 1 else None
            md_parts.append(grid_to_markdown(grid, label))
        except Exception as e:
            print(f'[sheet] Failed to read "{sheet["name"]}": {e}', file=sys.stderr)
            if len(selected) == 1:
                raise
            md_parts.append(f'## {sheet["name"]}\n\n*Error: {e}*')
    print('\n\n'.join(md_parts))


def cmd_sheet_download(cookies, input_str: str, opts: dict = None):
    """Download a spreadsheet as .xlsx via Feishu's native server-side export.

    Uses sheet_export.export_sheet_xlsx — same 3-step flow the browser's
    download menu hits, so cell types (number vs string) and styling
    (numberFormat: dates / percent / currency) are server-rendered into the
    xlsx, not reconstructed from REST text values. Default scope is the full
    workbook, matching Feishu's browser download. `--all` is accepted as a
    backward-compatible no-op; `--sheet NAME` filters to one sheet post-
    download with openpyxl.
    """
    from .. import sheet_export

    opts = opts or {}
    info = resolve_sheet_token(cookies, input_str)
    spreadsheet_token = info['spreadsheetToken']
    print(f'[sheet] Exporting {spreadsheet_token} ...', file=sys.stderr)
    xlsx_bytes = sheet_export.export_sheet_xlsx(cookies, spreadsheet_token)

    from ..paths import resolve_output_path
    title = spreadsheet_token
    try:
        meta_res = http_get(cookies, DOC_HOST, f'/space/api/meta/?token={spreadsheet_token}&type=3')
        if (meta_res.get('data') or {}).get('code') == 0:
            title = (meta_res['data']['data'].get('title') or '').replace('/', '_').strip() or spreadsheet_token
    except Exception:
        pass
    output_path = resolve_output_path(opts.get('outputPath'), f'{title}.xlsx')

    # Default / --all: preserve the exact server-rendered workbook bytes.
    # Re-saving through openpyxl strips Feishu/Excel extensions such as
    # comments, threaded comments, drawings, freeze panes, and some metadata.
    if not opts.get('sheetName'):
        with open(output_path, 'wb') as f:
            f.write(xlsx_bytes)
        kept = _xlsx_sheet_names(xlsx_bytes)
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
        sheet_name = opts.get('sheetName')
        target = sheet_name if sheet_name in wb.sheetnames else None
        if target is None:
            # Try numeric index (mirrors _select_sheets behavior)
            try:
                target = wb.sheetnames[int(sheet_name)]
            except (ValueError, IndexError):
                available = ', '.join(wb.sheetnames)
                raise RuntimeError(f'Sheet not found: "{sheet_name}". Available: {available}')
        for name in list(wb.sheetnames):
            if name != target:
                wb.remove(wb[name])
        wb.save(output_path)
        kept = [target]

    abs_path = os.path.abspath(output_path)
    print(json.dumps({
        'success': True, 'path': abs_path,
        'sheets': [{'sheet': n} for n in kept],
    }, indent=2))


# ── sheet images ─────────────────────────────────────────────────────────

def cmd_sheet_images(cookies, input_str: str, opts: dict = None):
    """List and optionally download images from a spreadsheet sheet.

    Usage from CLI:  lark sheet images <url> [--sheet NAME] [--cell A1] [--download] [--out PATH]
    Usage from agent: cmd_sheet_images(cookies, url, opts)

    Output (JSON):
      - spreadsheetToken, sheetId, sheetName
      - dims: {rows, cols}
      - images: [{index, token, width, height, cell}]
      - downloaded: [{token, cell, path, size}]  (when --download)

    When --cell is specified, only images in that cell are listed/downloaded.
    When --download is specified, images are downloaded to the output directory.
    """
    opts = opts or {}
    info = resolve_sheet_token(cookies, input_str)
    spreadsheet_token = info['spreadsheetToken']
    print(f'[sheet] Fetching sheet list for {spreadsheet_token}...', file=sys.stderr)
    sheets = fetch_sheet_list(cookies, spreadsheet_token)
    selected = _select_sheets(sheets, opts)
    sheet = selected[0]
    sheet_name = sheet['name']
    sheet_id = sheet['sheetId']

    print(f'[sheet] Extracting images from "{sheet_name}" ({sheet_id})...', file=sys.stderr)
    img_data = fetch_sheet_images(cookies, spreadsheet_token, sheet_id)

    target_cell = opts.get('cell')
    if target_cell:
        try:
            target_row, target_col = _parse_cell_range(target_cell)
        except ValueError as e:
            raise RuntimeError(str(e))

    # Build enriched image list with cell addresses
    enriched = []
    for img in img_data['images']:
        # f2 values in cell_map are 0-based, matching img['index']
        cells = img_data['cell_map'].get(img['index'], [])
        if target_cell:
            if target_cell not in cells:
                continue
            cells = [target_cell]
        enriched.append({
            'index': img['index'],
            'token': img['token'],
            'width': img['width'],
            'height': img['height'],
            'cells': cells,
        })

    output = {
        'spreadsheetToken': spreadsheet_token,
        'sheetId': sheet_id,
        'sheetName': sheet_name,
        'dims': img_data['dims'],
        'images': enriched,
    }

    # Download if requested
    if opts.get('download'):
        from ..commands.img import download_sheet_image
        from ..paths import resolve_output_path

        out_base = opts.get('outputPath') or ''
        downloaded = []
        for img in enriched:
            if not img['token'] or not img['cells']:
                continue
            cell_label = img['cells'][0].replace(' ', '_')
            default_name = f'{sheet_name}_{cell_label}_{img["token"][:12]}.png'
            out_path = resolve_output_path(out_base, default_name)
            try:
                download_sheet_image(cookies, img['token'], spreadsheet_token, out_path)
                downloaded.append({
                    'token': img['token'],
                    'cell': img['cells'][0],
                    'path': os.path.abspath(out_path),
                    'size': os.path.getsize(out_path),
                })
            except Exception as e:
                downloaded.append({
                    'token': img['token'],
                    'cell': img['cells'][0],
                    'error': str(e),
                })
        output['downloaded'] = downloaded

    print(json.dumps(output, indent=2))
