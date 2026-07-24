"""
docx_ot.py - Markdown → docx OT operation builder (Phase 1, write-only).

Bypasses easysync changeset encoding by writing each block's full text via
`action.oi` on `text.initialAttributedTexts`. Covers the subset needed for
"Claude writes a doc": headings 1-6, paragraphs, bullets, ordered lists,
code fences (with language), blockquote, divider.

Limitations (Phase 2 will add):
- No inline styling (bold/italic/underline/link/@mention)
- No tables, images, todos
"""

from __future__ import annotations

import os
import re
import string
import uuid as _uuid
from typing import Iterator

from .easysync import (
    AttribPool, encode_link_value, normalise_attrs, to_base36, utf16_length,
)

_BLOCK_ID_ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase  # base62


def _new_block_id() -> str:
    """Client-generated block ID. Feishu's real client uses 27-char base62
    strings (e.g. 'MMP3dfQd7odciexys9OcLupWnxe'); we match that format."""
    raw = os.urandom(24)
    s = []
    n = int.from_bytes(raw, 'big')
    while n and len(s) < 27:
        n, r = divmod(n, 62)
        s.append(_BLOCK_ID_ALPHABET[r])
    # pad if needed
    while len(s) < 27:
        s.append(_BLOCK_ID_ALPHABET[0])
    return ''.join(s[:27])


def _text_payload(text: str, author_uid: str, parse_inline: bool = False) -> dict:
    """Build the `text.initialAttributedTexts` struct for a text block.

    Each key in `text`/`attribs` represents one line. Within a line, `attribs`
    holds a sequence of `*N+<len>` groups using the same opcode language as a
    changeset — which lets a line carry multiple styled runs sharing one apool.

    When `parse_inline=True`, the caller's text is first run through
    `parse_inline_markdown` so `**bold**`, `*italic*`, `__underline__`,
    `~~strike~~`, `` `code` `` and `[label](url)` become styled runs.
    """
    text = text or ''
    if not text:
        return {
            'apool': {'nextNum': 0, 'numToAttrib': {}, 'attribToNum': {}},
            'initialAttributedTexts': {'attribs': {'0': ''}, 'text': {'0': ''}},
        }

    pool = AttribPool()
    text_map: dict[str, str] = {}
    attribs_map: dict[str, str] = {}

    for idx, line in enumerate(text.split('\n')):
        text_map[str(idx)] = line
        if not line:
            attribs_map[str(idx)] = ''
            continue

        runs = parse_inline_markdown(line) if parse_inline else [(line, {})]
        attribs_map[str(idx)] = _encode_line_attribs(runs, author_uid, pool)
        # text_map stores the *rendered* line (markdown markers stripped) so
        # the server sees the same string lengths that attribs describe.
        text_map[str(idx)] = ''.join(r[0] for r in runs)

    return {
        'apool': pool.to_dict(),
        'initialAttributedTexts': {
            'attribs': attribs_map,
            'text': text_map,
        },
    }


def _multiline_text_payload(text: str, author_uid: str) -> dict:
    """Code/caption-style payload: a single key `"0"` containing the full text
    with embedded newlines, attribs using `|N*0+M` — N = newline count,
    M = UTF-16 length of the entire string.

    Ensures a trailing `\\n` so the final paragraph has a boundary (matches
    captured behaviour: every code-block line is newline-terminated).
    """
    if not text:
        text = ''
    if not text.endswith('\n'):
        text = text + '\n'
    newline_count = text.count('\n')
    total_len = utf16_length(text)
    pool = AttribPool()
    author_num = pool.intern('author', author_uid)
    attr_prefix = f'|{to_base36(newline_count)}*{to_base36(author_num)}+{to_base36(total_len)}'
    return {
        'apool': pool.to_dict(),
        'initialAttributedTexts': {
            'text': {'0': text},
            'attribs': {'0': attr_prefix},
        },
    }


def _encode_line_attribs(
    runs: list[tuple[str, dict]],
    author_uid: str,
    pool: AttribPool,
) -> str:
    """Emit the changeset-fragment string for one line (no Z: prefix, no delta).

    Format: `*A*B+<len>*C+<len>…`. Each run contributes its own attrib ops +
    insert length. Runs without content are skipped.
    """
    parts: list[str] = []
    for text, attrs in runs:
        if not text:
            continue
        attr_nums = [pool.intern(k, v) for k, v in normalise_attrs(attrs, author_uid)]
        parts.extend(f'*{to_base36(n)}' for n in sorted(attr_nums, reverse=True))
        parts.append(f'+{to_base36(utf16_length(text))}')
    return ''.join(parts)


# ---------------------------------------------------------------------------
# Inline markdown → styled runs
# ---------------------------------------------------------------------------

# Ordered by specificity — link and code must be matched before bold/italic so
# their internal asterisks/underscores don't trigger styling.
_INLINE_RE = re.compile(
    r'(?P<code>`[^`\n]+?`)'
    r'|(?P<mdoc>@\[(?P<mdoc_title>[^\]]*)\]\((?P<mdoc_url>[^)\s]+)\))'
    r'|(?P<link>\[(?P<link_text>[^\]]+)\]\((?P<link_url>[^)\s]+)\))'
    r'|(?P<bold>\*\*[^*\n]+?\*\*)'
    r'|(?P<bold_u>__[^_\n]+?__)'
    r'|(?P<strike>~~[^~\n]+?~~)'
    r'|(?P<italic>(?<![\\*\w])\*[^*\n]+?\*(?!\*))'
    r'|(?P<italic_u>(?<![\\_\w])_[^_\n]+?_(?!_))'
)


_DOCX_URL_RE = re.compile(r'/(docx|wiki|doc)/([A-Za-z0-9]+)')


def _parse_mention_target(url_or_token: str) -> tuple[str, str]:
    """Return (token, raw_url) from either a feishu URL or a raw obj_token.

    For mention_doc, raw_url must be the full https URL; if the caller gave
    just a token we reconstruct the canonical docx URL.
    """
    m = _DOCX_URL_RE.search(url_or_token)
    if m:
        return m.group(2), url_or_token
    return url_or_token, f'https://nio.feishu.cn/docx/{url_or_token}'


def parse_inline_markdown(line: str) -> list[tuple[str, dict]]:
    """Split a single line into `[(text, attrs), ...]` runs.

    Supports: `**bold**` / `__bold__`, `*italic*` / `_italic_`, `~~strike~~`,
    `` `code` ``, `[label](url)`. Unrecognised markup passes through as plain
    text. Backslash-escapes a literal marker (`\\*` → `*`).
    """
    if '\\' in line:
        line = _normalise_escapes(line)

    runs: list[tuple[str, dict]] = []
    pos = 0
    for m in _INLINE_RE.finditer(line):
        if m.start() > pos:
            runs.append((line[pos:m.start()], {}))

        if m.group('code'):
            raw = m.group('code')
            runs.append((raw[1:-1], {'inlineCode': True}))
        elif m.group('mdoc'):
            token, raw_url = _parse_mention_target(m.group('mdoc_url'))
            runs.append((' ', {'mention_doc': {
                'token': token,
                'title': m.group('mdoc_title') or '',
                'raw_url': raw_url,
            }}))
        elif m.group('link'):
            runs.append((m.group('link_text'), {'link': m.group('link_url')}))
        elif m.group('bold') or m.group('bold_u'):
            raw = m.group('bold') or m.group('bold_u')
            runs.append((raw[2:-2], {'bold': True}))
        elif m.group('strike'):
            raw = m.group('strike')
            runs.append((raw[2:-2], {'strikethrough': True}))
        elif m.group('italic') or m.group('italic_u'):
            raw = m.group('italic') or m.group('italic_u')
            runs.append((raw[1:-1], {'italic': True}))

        pos = m.end()

    if pos < len(line):
        runs.append((line[pos:], {}))

    cleaned = [(_restore_escapes(t), a) for t, a in runs]
    return [(t, a) for t, a in cleaned if t] or [(_restore_escapes(line), {})]


# Private-use Unicode sentinels that the inline regex will never match on.
# Each escapable marker gets its own sentinel so we can round-trip unambiguously.
_ESCAPE_SENTINELS = {
    '*': '\uE000', '_': '\uE001', '~': '\uE002', '`': '\uE003',
    '[': '\uE004', ']': '\uE005', '\\': '\uE006',
}
_UNESCAPE = {v: k for k, v in _ESCAPE_SENTINELS.items()}


def _normalise_escapes(line: str) -> str:
    """Replace backslash-escaped markers with private-use sentinels so the
    inline regex skips them. `parse_inline_markdown` reverses this at the end."""
    out: list[str] = []
    i = 0
    while i < len(line):
        if line[i] == '\\' and i + 1 < len(line) and line[i + 1] in _ESCAPE_SENTINELS:
            out.append(_ESCAPE_SENTINELS[line[i + 1]])
            i += 2
        else:
            out.append(line[i])
            i += 1
    return ''.join(out)


def _restore_escapes(text: str) -> str:
    for sentinel, ch in _UNESCAPE.items():
        text = text.replace(sentinel, ch)
    return text


# Block "oi" templates. Only fields the server actually requires are set.
def _new_block_oi(
    btype: str,
    parent_id: str,
    author_uid: str,
    text: str,
    extra: dict = None,
    parse_inline: bool = True,
) -> dict:
    oi = {
        'parent_id': parent_id,
        'type': btype,
        'children': [],
        'comments': [],
        'revisions': [],
        'author': author_uid,
        'text': _text_payload(text, author_uid, parse_inline=parse_inline),
        'folded': False,
    }
    if extra:
        oi.update(extra)
    return oi


def _code_block_oi(parent_id: str, author_uid: str, text: str, language: str = '') -> dict:
    # Code blocks must preserve literal text AND use Etherpad paragraph-marker
    # format `|N*0+M` in a single key (not per-line keys), otherwise the
    # frontend only renders the first line. Verified against capture
    # /tmp/feishu_capture_blocks.json[111].
    oi = _new_block_oi('code', parent_id, author_uid, '', parse_inline=False)
    oi['text'] = _multiline_text_payload(text, author_uid)
    # Feishu uses human-readable display names for code language, capitalised.
    # Fall back to "Plain Text" (the Feishu default) if not provided or unknown.
    oi['language'] = _normalise_language(language)
    oi['is_language_picked'] = bool(language)
    oi['wrap'] = False
    # empty caption shape — verified against real capture
    # /tmp/feishu_capture_blocks.json[111] (text→code conversion).
    # Format: wrapped in outer `text`; initialAttributedTexts carries a
    # single newline char + Etherpad paragraph marker `|1+1` (paragraph
    # boundary + insert 1). An empty-string caption is rejected as
    # null-filled and makes the block fail to load.
    oi['caption'] = {
        'text': {
            'apool': {'numToAttrib': {}, 'nextNum': 0},
            'initialAttributedTexts': {
                'text': {'0': '\n'},
                'attribs': {'0': '|1+1'},
            },
        },
    }
    return oi


_LANGUAGE_MAP = {
    '': 'Plain Text', 'plain': 'Plain Text', 'text': 'Plain Text',
    'python': 'Python', 'py': 'Python',
    'javascript': 'JavaScript', 'js': 'JavaScript',
    'typescript': 'TypeScript', 'ts': 'TypeScript',
    'bash': 'Bash', 'sh': 'Shell', 'shell': 'Shell', 'zsh': 'Shell',
    'go': 'Go', 'golang': 'Go',
    'rust': 'Rust', 'rs': 'Rust',
    'java': 'Java', 'kotlin': 'Kotlin', 'swift': 'Swift',
    'c': 'C', 'cpp': 'C++', 'c++': 'C++', 'csharp': 'C#', 'cs': 'C#',
    'ruby': 'Ruby', 'rb': 'Ruby',
    'php': 'PHP',
    'sql': 'SQL', 'html': 'HTML', 'css': 'CSS', 'scss': 'SCSS',
    'json': 'JSON', 'yaml': 'YAML', 'yml': 'YAML', 'toml': 'TOML', 'xml': 'XML',
    'markdown': 'Markdown', 'md': 'Markdown',
}


def _normalise_language(lang: str) -> str:
    key = (lang or '').strip().lower()
    return _LANGUAGE_MAP.get(key, lang or 'Plain Text')


_TABLE_MAX_WIDTH_PX = 850   # PC 端飞书 docx 正文宽度的近似上限
_TABLE_MIN_COL_PX = 80      # 一个汉字+padding 的最小可读宽度
_TABLE_MAX_COL_PX = 300     # 单列再宽就该换行了
_TABLE_CELL_PADDING_PX = 16


def _estimate_text_px(s: str) -> int:
    """正文字号下文本渲染宽度的粗估（PingFang SC ~14px 体下：CJK ~16px，ASCII ~8px）。"""
    cjk = sum(1 for c in s if '一' <= c <= '鿿')
    return cjk * 16 + (len(s) - cjk) * 8


def _compute_column_widths(rows: list[list[str]]) -> list[int]:
    """按内容估算 + 总宽兜底的列宽计算，返回每列像素值。

    规则见对话讨论：先按最长 cell 估算每列宽度并 clamp 到 [80, 300]；
    若总宽超 850px：列数能塞下时按比例压缩，列数太多时降到 80px 任其水平滚动。
    """
    if not rows or not rows[0]:
        return []
    n_cols = len(rows[0])
    base: list[int] = []
    for c in range(n_cols):
        longest = max(_estimate_text_px(row[c]) for row in rows if c < len(row))
        base.append(max(_TABLE_MIN_COL_PX, min(_TABLE_MAX_COL_PX, longest + _TABLE_CELL_PADDING_PX)))
    total = sum(base)
    if total <= _TABLE_MAX_WIDTH_PX:
        return base
    if n_cols * _TABLE_MIN_COL_PX >= _TABLE_MAX_WIDTH_PX:
        return [_TABLE_MIN_COL_PX] * n_cols
    scale = _TABLE_MAX_WIDTH_PX / total
    return [max(_TABLE_MIN_COL_PX, int(b * scale)) for b in base]


def _table_container_oi(
    parent_id: str,
    author_uid: str,
    columns_id: list[str],
    rows_id: list[str],
    cell_grid: list[list[str]],
    column_widths: list[int],
) -> dict:
    """Build the `table` block OI.

    `cell_grid[row][col]` = the inner `table_cell` block_id at that position.
    Keys in `cell_set` are `rowId + colId` concatenated (not nested, not
    delimited) — see feishu_docx_block_schemas.md §6.2.
    """
    cell_set: dict = {}
    for r_idx, row_id in enumerate(rows_id):
        for c_idx, col_id in enumerate(columns_id):
            cell_set[row_id + col_id] = {
                'block_id': cell_grid[r_idx][c_idx],
                'merge_info': {'row_span': 1, 'col_span': 1},
            }
    return {
        'parent_id': parent_id,
        'type': 'table',
        'author': author_uid,
        'children': [],
        'comments': [],
        'revisions': [],
        'columns_id': columns_id,
        'rows_id': rows_id,
        'column_set': {
            col_id: {'column_width': column_widths[i]}
            for i, col_id in enumerate(columns_id)
        },
        'cell_set': cell_set,
    }


def _table_cell_oi(parent_id: str, author_uid: str, text_child_id: str) -> dict:
    """A table_cell has no fields of its own — content lives in its text child."""
    return {
        'parent_id': parent_id,
        'type': 'table_cell',
        'author': author_uid,
        'children': [text_child_id],
        'comments': [],
        'revisions': [],
    }


def _divider_oi(parent_id: str, author_uid: str) -> dict:
    return {
        'parent_id': parent_id,
        'type': 'divider',
        'children': [],
        'comments': [],
        'revisions': [],
        'author': author_uid,
    }


# ---------------------------------------------------------------------------
# Markdown → block list
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)$')
_BULLET_RE = re.compile(r'^[-*+]\s+(.*)$')
_ORDERED_RE = re.compile(r'^\d+\.\s+(.*)$')
_QUOTE_RE = re.compile(r'^>\s?(.*)$')
_FENCE_RE = re.compile(r'^```(\w*)\s*$')
_DIVIDER_RE = re.compile(r'^(-{3,}|\*{3,}|_{3,})\s*$')
_TABLE_ROW_RE = re.compile(r'^\|.*\|\s*$')
_TABLE_SEP_CELL_RE = re.compile(r'^\s*:?-{3,}:?\s*$')


def _split_table_row(line: str) -> list[str]:
    """Split `| a | b | c |` → ['a', 'b', 'c']. Handles backslash-escaped `\\|`."""
    # temporarily replace escaped pipes
    s = line.strip().replace('\\|', '\x00')
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|'):
        s = s[:-1]
    return [c.strip().replace('\x00', '|') for c in s.split('|')]


def _is_table_separator(line: str) -> bool:
    """A GFM table separator row: `|---|:---:|---|`."""
    if not _TABLE_ROW_RE.match(line):
        return False
    cells = _split_table_row(line)
    return bool(cells) and all(_TABLE_SEP_CELL_RE.match(c) for c in cells)


def parse_markdown(md: str) -> list[dict]:
    """Parse a subset of Markdown into a list of logical blocks.

    Each block is {'type': <docx_type>, 'text': <str>, 'language': <str|None>}.
    Consecutive paragraph lines are joined with a space (hard wraps become soft).
    """
    blocks: list[dict] = []
    lines = md.splitlines()
    i = 0
    paragraph: list[str] = []

    def flush_paragraph():
        if paragraph:
            blocks.append({'type': 'text', 'text': ' '.join(paragraph).strip()})
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            i += 1
            continue

        # Code fence
        m_fence = _FENCE_RE.match(stripped)
        if m_fence:
            flush_paragraph()
            lang = m_fence.group(1)
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE_RE.match(lines[i].strip()):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):  # skip closing fence
                i += 1
            blocks.append({'type': 'code', 'text': '\n'.join(code_lines), 'language': lang})
            continue

        # Divider
        if _DIVIDER_RE.match(stripped):
            flush_paragraph()
            blocks.append({'type': 'divider', 'text': ''})
            i += 1
            continue

        # Table: header row + separator row + >=0 body rows. Must peek ahead
        # to confirm the separator; a lone `|foo|` line is treated as text.
        if (
            _TABLE_ROW_RE.match(stripped)
            and i + 1 < len(lines)
            and _is_table_separator(lines[i + 1].strip())
        ):
            flush_paragraph()
            header_cells = _split_table_row(stripped)
            i += 2  # skip header + separator
            rows: list[list[str]] = [header_cells]
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i].strip()):
                rows.append(_split_table_row(lines[i].strip()))
                i += 1
            # Normalise ragged rows to match header column count.
            width = len(header_cells)
            rows = [r + [''] * (width - len(r)) if len(r) < width else r[:width] for r in rows]
            blocks.append({'type': 'table', 'rows': rows})
            continue

        # Heading
        m_head = _HEADING_RE.match(stripped)
        if m_head:
            flush_paragraph()
            level = len(m_head.group(1))
            blocks.append({'type': f'heading{level}', 'text': m_head.group(2).strip()})
            i += 1
            continue

        # Bullet
        m_bullet = _BULLET_RE.match(stripped)
        if m_bullet:
            flush_paragraph()
            blocks.append({'type': 'bullet', 'text': m_bullet.group(1).strip()})
            i += 1
            continue

        # Ordered
        m_ord = _ORDERED_RE.match(stripped)
        if m_ord:
            flush_paragraph()
            blocks.append({'type': 'ordered', 'text': m_ord.group(1).strip()})
            i += 1
            continue

        # Quote
        m_quote = _QUOTE_RE.match(stripped)
        if m_quote:
            flush_paragraph()
            blocks.append({'type': 'quote', 'text': m_quote.group(1).strip()})
            i += 1
            continue

        paragraph.append(stripped)
        i += 1

    flush_paragraph()
    return blocks


# ---------------------------------------------------------------------------
# Block list → change_map (user_change payload)
# ---------------------------------------------------------------------------

def _add_table_blocks(
    change_map: dict,
    rows: list[list[str]],
    parent_id: str,
    author_uid: str,
    column_widths: list[int] | None = None,
) -> str:
    """Materialise a table as:
        table (parent_id=<doc root>)
          ├─ table_cell[row0,col0] (parent_id=table)
          │    └─ text (parent_id=cell)
          ├─ table_cell[row0,col1] …
          └─ …
    Writes every block's `oi` into change_map and returns the table block_id.

    `column_widths`: 显式指定每列像素宽度；None 则用 _compute_column_widths(rows) 按内容估算。
    """
    n_rows = len(rows)
    n_cols = len(rows[0]) if rows else 0
    table_id = _new_block_id()
    columns_id = [f'col{_uuid.uuid4()}' for _ in range(n_cols)]
    rows_id = [f'row{_uuid.uuid4()}' for _ in range(n_rows)]
    if column_widths is None:
        column_widths = _compute_column_widths(rows)

    cell_grid: list[list[str]] = [[_new_block_id() for _ in range(n_cols)] for _ in range(n_rows)]

    # Build inner text children first, then cells, then the table container.
    # change_map is flat — order inside it is irrelevant to the server, but we
    # emit leaves first to keep the intent readable.
    for r_idx in range(n_rows):
        for c_idx in range(n_cols):
            cell_id = cell_grid[r_idx][c_idx]
            text_id = _new_block_id()
            cell_text = rows[r_idx][c_idx]

            change_map[text_id] = {
                'id': text_id,
                'version': 0,
                'payload': {
                    'ops': [{
                        'p': [],
                        'action': {'oi': _new_block_oi('text', cell_id, author_uid, cell_text)},
                    }],
                },
            }
            change_map[cell_id] = {
                'id': cell_id,
                'version': 0,
                'payload': {
                    'ops': [{
                        'p': [],
                        'action': {'oi': _table_cell_oi(table_id, author_uid, text_id)},
                    }],
                },
            }

    change_map[table_id] = {
        'id': table_id,
        'version': 0,
        'payload': {
            'ops': [{
                'p': [],
                'action': {'oi': _table_container_oi(
                    parent_id, author_uid, columns_id, rows_id, cell_grid,
                    column_widths,
                )},
            }],
        },
    }
    return table_id


def build_insert_change_map(
    blocks: list[dict],
    parent_id: str,
    parent_version: int,
    author_uid: str,
    insert_at: int,
) -> tuple[dict, list[str]]:
    """Build a change_map that:
      1. Creates each new block (oi on p:[])
      2. Appends each new block's id to parent's children list (li)

    Args:
        blocks: output from parse_markdown
        parent_id: target docx page_id (or container block id)
        parent_version: current version of the parent block
        author_uid: current user's uid (for `author` field on blocks)
        insert_at: index in parent's children to start inserting at

    Returns:
        (change_map, [new_block_ids])
    """
    if not blocks:
        return {}, []

    change_map: dict = {}
    new_ids: list[str] = []

    for blk in blocks:
        btype = blk['type']

        if btype == 'table':
            table_id = _add_table_blocks(change_map, blk['rows'], parent_id, author_uid)
            new_ids.append(table_id)
            continue

        bid = _new_block_id()
        new_ids.append(bid)
        text = blk.get('text') or ''

        if btype == 'divider':
            oi = _divider_oi(parent_id, author_uid)
        elif btype == 'code':
            oi = _code_block_oi(parent_id, author_uid, text, blk.get('language') or '')
        else:
            # heading1..6, bullet, ordered, quote, text
            oi = _new_block_oi(btype, parent_id, author_uid, text)

        change_map[bid] = {
            'id': bid,
            'version': 0,  # new block — client hasn't seen it yet
            'payload': {
                'ops': [{'p': [], 'action': {'oi': oi}}],
            },
        }

    # Attach new children to parent in order, starting at insert_at.
    parent_ops = []
    for offset, bid in enumerate(new_ids):
        parent_ops.append({'p': ['children', insert_at + offset], 'action': {'li': bid}})
    change_map[parent_id] = {
        'id': parent_id,
        'version': parent_version,
        'payload': {'ops': parent_ops},
    }

    return change_map, new_ids
