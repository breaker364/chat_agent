"""
commands/doc.py - Document read commands (mirrors lib/commands/doc.js)
"""

import base64
import json
import os
import re
import sys
import zlib

from ..config import DOC_HOST, DOC_IMAGE_HOST
from ..http_utils import http_get, http_get_binary, http_post_json, cookies_to_dict
from ..proto import decode_varint


# ---------------------------------------------------------------------------
# Internal protobuf raw-field parser (mirrors local decodeProto in doc.js)
# Returns dict {fN: bytes|int|list}, raw bytes for length-delimited fields
# ---------------------------------------------------------------------------

from ..sheet_decoder import decode_proto_raw as _decode_proto_raw  # noqa: F401


# ---------------------------------------------------------------------------
# fetch_sheet_markdown_table
# ---------------------------------------------------------------------------

def fetch_sheet_markdown_table(cookies, sheet_full_token: str) -> str:
    parts = sheet_full_token.split('_')
    if len(parts) < 2:
        return f'![sheet](https://nio.feishu.cn/sheets/{sheet_full_token})'
    spreadsheet_token, sheet_id = parts[0], parts[1]

    try:
        cookie_str = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
        body = json.dumps({
            'memberId': 0, 'schemaVersion': 9, 'openType': 1,
            'token': spreadsheet_token,
            'sheetRange': {'sheetId': sheet_id},
            'clientVersion': 'v0.0.1',
        })
        res = http_post_json(
            f'https://{DOC_HOST}/space/api/v3/sheet/client_vars',
            json.loads(body),
            {'Cookie': cookie_str, 'Referer': f'https://{DOC_HOST}/'},
        )
        data = res.get('body') if isinstance(res, dict) else None
        if not data or data.get('code') != 0 or not data.get('data', {}).get('snapshot'):
            raise ValueError('Sheet API returned no data')

        snapshot = data['data']['snapshot']
        block_keys = list((snapshot.get('blocks') or {}).keys())
        if not block_keys:
            raise ValueError('No cell blocks')

        cell_buf = zlib.decompress(base64.b64decode(snapshot['blocks'][block_keys[0]]))
        top = _decode_proto_raw(cell_buf)
        inner = _decode_proto_raw(top.get('f1', b''))
        f2_data = _decode_proto_raw(inner.get('f2', b''))
        f12 = _decode_proto_raw(f2_data.get('f12', b''))
        meta = _decode_proto_raw(f12.get('f1', b''))

        ROWS = meta.get('f3', 0)
        COLS = meta.get('f4', 0)
        if not ROWS or not COLS:
            raise ValueError('Empty sheet')
        if isinstance(ROWS, bytes):
            ROWS, _ = decode_varint(ROWS, 0)
        if isinstance(COLS, bytes):
            COLS, _ = decode_varint(COLS, 0)

        # Parse f12.f2: text entries (fn=2→str) and rich entries (fn=3→{textIdx})
        text_entries, rich_entries = [], []
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
                    if fn == 2:
                        text_entries.append(chunk.decode('utf-8', errors='replace'))
                    elif fn == 3:
                        rr = _decode_proto_raw(chunk)
                        text_idx = rr.get('f1', -1)
                        if isinstance(text_idx, bytes):
                            text_idx, _ = decode_varint(bytes(text_idx), 0)
                        rich_entries.append({'textIdx': text_idx})
                elif wt == 0:
                    _, pos = decode_varint(f2_raw, pos)
                else:
                    break
            except Exception:
                break

        # Parse f12.f6: f1=indexMap bytes, f2=cellMeta entries
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
                        cell_meta.append({k: (v if not isinstance(v, bytes) else int.from_bytes(v, 'little')) for k, v in entry.items()})
                elif wt == 0:
                    _, pos = decode_varint(f6_raw, pos)
                else:
                    break
            except Exception:
                break

        if not index_map or len(index_map) < ROWS * COLS:
            raise ValueError('Index map size mismatch')

        grid = []
        seen = set()
        for r in range(ROWS):
            row = []
            for c in range(COLS):
                idx = index_map[r * COLS + c]
                if idx == 0 or idx >= len(cell_meta):
                    row.append('')
                    continue
                if idx in seen:
                    row.append('')
                    continue
                seen.add(idx)
                cm = cell_meta[idx]
                f1 = cm.get('f1', 0)
                f2 = cm.get('f2')
                if f1 == 3 and f2 is not None:
                    row.append(text_entries[f2] if f2 < len(text_entries) else '')
                elif f1 == 6 and f2 is not None:
                    rich = rich_entries[f2] if f2 < len(rich_entries) else None
                    ti = rich['textIdx'] if rich else -1
                    row.append(text_entries[ti] if rich and 0 <= ti < len(text_entries) else '')
                else:
                    row.append('')
            grid.append(row)

        # Drop trailing empty rows
        while len(grid) > 1 and all(c == '' for c in grid[-1]):
            grid.pop()

        def esc(s):
            return s.replace('\n', ' ').replace('|', '\\|').strip()

        lines = ['| ' + ' | '.join(esc(c) for c in grid[0]) + ' |',
                 '| ' + ' | '.join('---' for _ in grid[0]) + ' |']
        for row in grid[1:]:
            lines.append('| ' + ' | '.join(esc(c) for c in row) + ' |')
        return '\n'.join(lines)

    except Exception as e:
        print(f'[sheet] Failed to parse sheet data: {e}, falling back to image', file=sys.stderr)
        img_url = f'https://nio.feishu.cn/space/api/file/f/cdp-sheet-{spreadsheet_token}~noop/'
        return f'![sheet]({img_url})'


# ---------------------------------------------------------------------------
# extract_block_text
# ---------------------------------------------------------------------------

def extract_block_text(block: dict) -> str:
    texts = (block.get('text') or {})
    iat = texts.get('initialAttributedTexts') or {}
    text_map = iat.get('text') or {}
    if not text_map:
        return ''
    parts = [text_map[k] for k in sorted(text_map.keys(), key=lambda x: int(x)) if text_map[k]]
    result = ''.join(parts)

    attrs = (texts.get('apool') or {}).get('numToAttrib') or {}
    for v in attrs.values():
        if not isinstance(v, list) or v[0] != 'inline-component':
            continue
        try:
            comp = json.loads(v[1])
            ctype = comp.get('type')
            data = comp.get('data') or {}
            if ctype == 'url_preview':
                title = data.get('title', '')
                url = data.get('raw_url', '')
                result = result.replace('\ufffc', f'[{title}]({url})', 1)
            elif ctype == 'user':
                result = result.replace('\ufffc', f"@user:{data.get('uid')}", 1)
            elif ctype == 'mention_doc':
                title = data.get('title', '')
                url = data.get('raw_url', '')
                # Captured mentions use a literal space as the visible char, not
                # \ufffc. Try both so we handle write-back from this CLI and
                # legacy/object-replacement-char mentions from the desktop client.
                marker = f'@[{title}]({url})'
                if '\ufffc' in result:
                    result = result.replace('\ufffc', marker, 1)
                else:
                    result = result.replace(' ', marker, 1)
        except Exception:
            pass
    return result.strip()


# ---------------------------------------------------------------------------
# doc_blocks_to_markdown
# ---------------------------------------------------------------------------

def doc_blocks_to_markdown(block_sequence, block_map, cookies=None) -> str:
    lines = []
    table_blocks = set()

    # Pre-scan: collect block IDs that are inside tables/quotes (skip in main loop)
    for bid in block_sequence:
        entry = block_map.get(bid)
        b = entry.get('data') if entry else None
        if not b:
            continue
        if b.get('type') == 'table':
            for cell_info in (b.get('cell_set') or {}).values():
                if cell_info.get('block_id'):
                    table_blocks.add(cell_info['block_id'])
            for cid in (b.get('children') or []):
                table_blocks.add(cid)
        if b.get('type') == 'table_cell':
            for cid in (b.get('children') or []):
                table_blocks.add(cid)
        if b.get('type') == 'quote_container':
            for cid in (b.get('children') or []):
                table_blocks.add(cid)

    for bid in block_sequence:
        if bid in table_blocks:
            continue
        entry = block_map.get(bid)
        if not entry or not entry.get('data'):
            continue
        b = entry['data']
        text = extract_block_text(b)
        btype = b.get('type', '')

        if btype == 'heading1':
            lines.append(f'# {text}')
        elif btype == 'heading2':
            lines.append(f'## {text}')
        elif btype == 'heading3':
            lines.append(f'### {text}')
        elif btype == 'heading4':
            lines.append(f'#### {text}')
        elif btype == 'heading5':
            lines.append(f'##### {text}')
        elif btype in ('heading6', 'heading7', 'heading8', 'heading9'):
            lines.append(f'###### {text}')
        elif btype == 'text':
            lines.append(text if text else '')
        elif btype == 'ordered':
            lines.append(f'1. {text}')
        elif btype == 'bullet':
            lines.append(f'- {text}')
        elif btype == 'todo':
            checked = 'x' if b.get('checked') else ' '
            lines.append(f'- [{checked}] {text}')
        elif btype == 'code':
            lang = (b.get('code') or {}).get('language', '')
            lines.append(f'```{lang}')
            lines.append(text)
            lines.append('```')
        elif btype == 'divider':
            lines.append('---')
        elif btype == 'image':
            img = b.get('image') or {}
            token = img.get('token', '')
            name = img.get('name', 'image')
            url = f'https://{DOC_IMAGE_HOST}/space/api/box/stream/download/v2/cover/{token}/?fallback_source=1&height=1280&mount_point=docx_image&policy=equal'
            lines.append(f'![{name}]({url})')
        elif btype == 'whiteboard':
            wb_token = b.get('token', '')
            lines.append(f'![whiteboard](https://nio.feishu.cn/space/api/file/f/cdp-whiteboard-{wb_token}~noop/)')
        elif btype == 'sheet':
            sheet_full_token = b.get('token', '')
            if cookies:
                lines.append(fetch_sheet_markdown_table(cookies, sheet_full_token))
            else:
                sheet_token = sheet_full_token.split('_')[0]
                lines.append(f'![sheet](https://nio.feishu.cn/space/api/file/f/cdp-sheet-{sheet_token}~noop/)')
        elif btype == 'table':
            rows_id = b.get('rows_id') or []
            cols_id = b.get('columns_id') or []
            cell_set = b.get('cell_set') or {}
            if rows_id and cols_id:
                for ri, row_id in enumerate(rows_id):
                    cells = []
                    for col_id in cols_id:
                        cell_key = row_id + col_id
                        cell_info = cell_set.get(cell_key)
                        cell_text = ''
                        if cell_info and cell_info.get('block_id'):
                            cb = block_map.get(cell_info['block_id'])
                            if cb and cb.get('data') and cb['data'].get('children'):
                                child_texts = [extract_block_text(block_map[c]['data']) for c in cb['data']['children'] if block_map.get(c) and block_map[c].get('data')]
                                cell_text = ' / '.join(t for t in child_texts if t)
                        cells.append(cell_text)
                    lines.append('| ' + ' | '.join(cells) + ' |')
                    if ri == 0:
                        lines.append('| ' + ' | '.join('---' for _ in cols_id) + ' |')
                lines.append('')
        elif btype == 'quote_container':
            for cid in (b.get('children') or []):
                cb = block_map.get(cid)
                if cb and cb.get('data'):
                    ct = extract_block_text(cb['data'])
                    if ct:
                        lines.append(f'> {ct}')
        else:
            if text:
                lines.append(text)

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# fetch_doc_blocks
# ---------------------------------------------------------------------------

def fetch_doc_blocks(cookies, docx_token: str) -> dict:
    all_blocks = {}
    all_sequence = []
    seen_ids = set()
    cursor = None
    page = 0

    while True:
        page += 1
        url = f'/space/api/docx/pages/client_vars?id={docx_token}&mode=7&limit=50'
        if cursor:
            from urllib.parse import quote
            url += f'&cursor={quote(cursor)}'
        print(f'[doc] Fetching page {page}...', file=sys.stderr)

        res = http_get(cookies, DOC_HOST, url)
        if not res.get('data') or res['data'].get('code') != 0:
            return {'error': 'Failed to fetch doc', 'response': res.get('data')}

        d = res['data'].get('data') or {}
        if d.get('block_map'):
            all_blocks.update(d['block_map'])
        for bid in (d.get('block_sequence') or []):
            if bid not in seen_ids:
                seen_ids.add(bid)
                all_sequence.append(bid)

        if d.get('has_more') and d.get('cursor'):
            cursor = d['cursor']
        else:
            break

    print(f'[doc] Total: {len(all_sequence)} blocks', file=sys.stderr)
    return {'blocks': all_blocks, 'sequence': all_sequence, 'pages': page}


# ---------------------------------------------------------------------------
# resolve_doc_token
# ---------------------------------------------------------------------------

def resolve_doc_token(cookies, token: str) -> str:
    is_wiki = False
    if '/' in token:
        m = re.search(r'/(wiki|docx|doc|base|sheets)/([A-Za-z0-9\-_]+)', token)
        if m:
            is_wiki = m.group(1) == 'wiki'
            token = m.group(2)

    if is_wiki:
        node_res = http_get(cookies, DOC_HOST, f'/space/api/wiki/v2/tree/get_node/?wiki_token={token}&expand_shortcut=true')
        node_data = (node_res.get('data') or {}).get('data') or {}
        if (node_res.get('data') or {}).get('code') == 0 and node_data.get('obj_token'):
            return node_data['obj_token']

    meta_res = http_get(cookies, DOC_HOST, f'/space/api/meta/?token={token}&type=22')
    if (meta_res.get('data') or {}).get('code') == 0:
        return token

    print(f'[doc] Trying wiki resolution for {token}...', file=sys.stderr)
    node_res = http_get(cookies, DOC_HOST, f'/space/api/wiki/v2/tree/get_node/?wiki_token={token}&expand_shortcut=true')
    node_data = (node_res.get('data') or {}).get('data') or {}
    if (node_res.get('data') or {}).get('code') == 0 and node_data.get('obj_token'):
        print(f"[doc] Resolved wiki → docx: {node_data['obj_token']}", file=sys.stderr)
        return node_data['obj_token']
    return token


# ---------------------------------------------------------------------------
# cmd_doc_read / cmd_doc_length / download_doc_images / cmd_doc_download / cmd_doc_meta
# ---------------------------------------------------------------------------

def cmd_doc_read(cookies, token: str):
    docx_token = resolve_doc_token(cookies, token)
    result = fetch_doc_blocks(cookies, docx_token)
    if result.get('error'):
        print(json.dumps(result))
        return
    markdown = doc_blocks_to_markdown(result['sequence'], result['blocks'], cookies)
    print(markdown)


def cmd_doc_blocks(
    cookies,
    token: str,
    type_filter: str = None,
    preview_chars: int = 60,
    grep: str = None,
    limit: int = None,
    offset: int = 0,
    fmt: str = 'json',
):
    """List each block's {id, type, language?, preview}. Used to discover
    block_ids for `doc edit` / `doc edit-code` / `doc delete-block`.

    Flags:
      type_filter  — only blocks of this exact type (e.g. `code`, `heading2`)
      grep         — regex/substring match against the extracted text
      limit/offset — pagination over the filtered result set
      fmt          — `json` (default) or `compact` (tab-separated, one per line)

    Container blocks (table / table_cell / quote_container) have their inner
    text children suppressed so the listing matches `doc read` visual order.
    """
    import re as _re
    docx_token = resolve_doc_token(cookies, token)
    result = fetch_doc_blocks(cookies, docx_token)
    if result.get('error'):
        print(json.dumps(result))
        return

    # Build a set of block_ids that are inner-text-children of containers.
    suppressed: set = set()
    for bid, entry in result['blocks'].items():
        b = (entry or {}).get('data') or {}
        btype = b.get('type', '')
        if btype == 'table':
            for ci in (b.get('cell_set') or {}).values():
                if ci.get('block_id'):
                    suppressed.add(ci['block_id'])
        if btype == 'table_cell' or btype == 'quote_container':
            for cid in (b.get('children') or []):
                suppressed.add(cid)

    grep_re = _re.compile(grep) if grep else None

    rows = []
    total_before_paging = 0
    for bid in result['sequence']:
        if bid in suppressed:
            continue
        entry = result['blocks'].get(bid) or {}
        b = entry.get('data') or {}
        if not b:
            continue
        btype = b.get('type', '')
        if type_filter and type_filter != btype:
            continue
        text = extract_block_text(b)
        if grep_re and not grep_re.search(text):
            continue
        total_before_paging += 1
        if total_before_paging <= offset:
            continue
        if limit is not None and len(rows) >= limit:
            continue
        label = btype
        if btype == 'code' and b.get('language'):
            label = f'code/{b["language"]}'
        preview = text.replace('\n', ' ⏎ ')[:preview_chars]
        rows.append({'block_id': bid, 'type': label, 'preview': preview})

    if fmt == 'compact':
        # tab-separated, LLM-friendly. No header so output is pure data.
        for r in rows:
            print(f"{r['block_id']}\t{r['type']}\t{r['preview']}")
        if total_before_paging > len(rows) + offset:
            print(f"# {total_before_paging} total matches; shown {len(rows)} after offset={offset} limit={limit}", file=sys.stderr)
        return

    payload = {
        'obj_token': docx_token,
        'count': len(rows),
        'blocks': rows,
    }
    if limit is not None or offset:
        payload['total_matches'] = total_before_paging
        payload['offset'] = offset
        payload['limit'] = limit
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_doc_length(cookies, token: str):
    docx_token = resolve_doc_token(cookies, token)
    result = fetch_doc_blocks(cookies, docx_token)
    if result.get('error'):
        print(json.dumps(result))
        return
    block_types = {}
    total_chars = 0
    for bid in result['sequence']:
        block = (result['blocks'].get(bid) or {}).get('data')
        if not block:
            continue
        t = block.get('type', 'unknown')
        block_types[t] = block_types.get(t, 0) + 1
        txt = extract_block_text(block)
        if txt:
            total_chars += len(txt)
    print(json.dumps({'success': True, 'blocks': len(result['sequence']), 'chars': total_chars, 'pages': result['pages'], 'blockTypes': block_types}, indent=2))


def download_doc_images(cookies, markdown: str, output_dir: str) -> dict:
    img_dir = os.path.join(output_dir, 'images')
    matches = list(re.finditer(r'!\[([^\]]*)\]\((https://[^)]+)\)', markdown))
    if not matches:
        return {'markdown': markdown, 'downloaded': 0}

    os.makedirs(img_dir, exist_ok=True)
    result = markdown
    downloaded = 0

    for i, m in enumerate(matches):
        full_match, name, url = m.group(0), m.group(1), m.group(2)
        try:
            print(f'[doc] Downloading image {i + 1}/{len(matches)}...', file=sys.stderr)
            resp = http_get_binary(cookies, url)
            if resp.get('status') != 200:
                print(f"[doc] Image {i + 1} failed: HTTP {resp.get('status')}", file=sys.stderr)
                continue
            buf = resp['buffer']
            magic = buf[:4].hex().upper() if len(buf) >= 4 else ''
            ext = '.png'
            if magic.startswith('FFD8FF'):
                ext = '.jpg'
            elif magic.startswith('47494638'):
                ext = '.gif'
            elif magic.startswith('52494646') and len(buf) >= 12 and buf[8:12] == b'WEBP':
                ext = '.webp'
            else:
                ct = resp.get('contentType', '')
                if 'jpeg' in ct or 'jpg' in ct:
                    ext = '.jpg'
                elif 'gif' in ct:
                    ext = '.gif'
                elif 'webp' in ct:
                    ext = '.webp'
                elif 'svg' in ct:
                    ext = '.svg'
            file_name = f'{i + 1}{ext}'
            file_path = os.path.join(img_dir, file_name)
            with open(file_path, 'wb') as f:
                f.write(buf)
            result = result.replace(full_match, f'![{name}](images/{file_name})', 1)
            downloaded += 1
        except Exception as e:
            print(f'[doc] Image {i + 1} error: {e}', file=sys.stderr)

    return {'markdown': result, 'downloaded': downloaded}


def cmd_doc_download(cookies, token: str, output_path=None, download_images=False):
    docx_token = resolve_doc_token(cookies, token)

    title = docx_token
    meta_res = http_get(cookies, DOC_HOST, f'/space/api/meta/?token={docx_token}&type=22')
    if (meta_res.get('data') or {}).get('code') == 0:
        title = ((meta_res['data'].get('data') or {}).get('title') or docx_token)

    result = fetch_doc_blocks(cookies, docx_token)
    if result.get('error'):
        print(json.dumps(result))
        return

    markdown = doc_blocks_to_markdown(result['sequence'], result['blocks'], cookies)

    from ..paths import resolve_output_path
    safe_name = re.sub(r'[/\\:*?"<>|]', '_', title).replace(' ', '_')[:100]
    output_path = resolve_output_path(output_path, f'{safe_name}.md')
    abs_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(abs_path)

    images_downloaded = 0
    if download_images:
        img_result = download_doc_images(cookies, markdown, output_dir)
        markdown = img_result['markdown']
        images_downloaded = img_result['downloaded']

    with open(abs_path, 'w', encoding='utf-8') as f:
        f.write(markdown)

    out = {'success': True, 'title': title, 'path': abs_path, 'blocks': len(result['sequence']), 'chars': len(markdown)}
    if download_images:
        out['imagesDownloaded'] = images_downloaded
    print(json.dumps(out, indent=2))


def cmd_doc_meta(cookies, token: str):
    # Resolve obj_type and obj_token from the URL or raw token, then read the
    # metadata (including sec_label / 密级) via the shared /space/api/meta/ API.
    # Supported URL path prefixes and their obj_type values:
    #   /docx/  → 22 (docx)
    #   /doc/   → 22 (docx alias)
    #   /wiki/  → resolved via wiki tree API to the real obj_type + obj_token
    #   /base/  → 8  (bitable / 多维表格)
    #   /sheets/→ 3  (spreadsheet)
    # Raw tokens (no '/') fall through to resolve_doc_token which probes
    # with type=22 and falls back to wiki resolution.
    #
    # IMPORTANT: /space/api/meta/ DOES return sec_label for bitable (type=8)
    # and sheet (type=3), as long as the correct obj_type is supplied. The
    # bitable clientvars API ("base" payload) does NOT carry sec_label, so it
    # must not be used here — doing so silently drops the confidentiality
    # label and breaks Langfuse 密级 upload for 多维表格 documents.
    obj_type = 22
    obj_token = token
    if '/' in token:
        # /base/ URLs: bitable (多维表格), obj_type=8
        m_base = re.search(r'/base/([A-Za-z0-9]+)', token)
        if m_base:
            obj_token = m_base.group(1)
            obj_type = 8
        # /sheets/ URLs: spreadsheet, obj_type=3
        elif re.search(r'/sheets/', token):
            m_sheets = re.search(r'/sheets/([A-Za-z0-9]+)', token)
            if m_sheets:
                obj_token = m_sheets.group(1)
                obj_type = 3
        # /wiki/ URLs: resolve via wiki tree API to get real obj_type + obj_token
        elif re.search(r'/wiki/', token):
            m = re.search(r'/wiki/([A-Za-z0-9]+)', token)
            if m:
                wiki_token = m.group(1)
                node_res = http_get(cookies, DOC_HOST, f'/space/api/wiki/v2/tree/get_node/?wiki_token={wiki_token}&expand_shortcut=true')
                node_data = (node_res.get('data') or {}).get('data') or {}
                if (node_res.get('data') or {}).get('code') == 0:
                    obj_type = node_data.get('obj_type', 22)
                    obj_token = node_data.get('obj_token', token)
        else:
            obj_token = resolve_doc_token(cookies, token)
    else:
        obj_token = resolve_doc_token(cookies, token)

    res = http_get(cookies, DOC_HOST, f'/space/api/meta/?token={obj_token}&type={obj_type}')
    if (res.get('data') or {}).get('code') == 0:
        print(json.dumps(res['data'].get('data'), indent=2))
    else:
        print(json.dumps({'error': 'Failed to fetch meta', 'response': res.get('data')}))
