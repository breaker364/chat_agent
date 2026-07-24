"""
easysync.py - Etherpad-style changeset encoder for Feishu docx text edits.

Feishu's `subType: "easysync"` payload describes text changes as a compact
base36-encoded op stream + an attribute pool. This module only builds
outgoing changesets (writer-side); decoding / OT merging is not implemented.

See: /Users/francis.wang2/francis_project/apidrift/references/feishu_docx_easysync_format.md

Supported attributes (by construction — other keys still encode fine if
passed through):
    bold / italic / underline / strikethrough / inlineCode → "true" / ""
    link → URL-encoded string
    author → uid string
"""

from __future__ import annotations

import json as _json
import uuid as _uuid
from typing import Iterable, Sequence
from urllib.parse import quote


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

_B36 = '0123456789abcdefghijklmnopqrstuvwxyz'


def to_base36(n: int) -> str:
    if n == 0:
        return '0'
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36[r])
    return ''.join(reversed(out))


def utf16_length(s: str) -> int:
    """Count UTF-16 code units — matches JavaScript `.length`.

    Chinese characters are 1 code unit; emoji and supplementary-plane
    characters count as 2. Feishu measures changeset lengths this way.
    """
    return sum(1 if ord(c) <= 0xFFFF else 2 for c in s)


# ---------------------------------------------------------------------------
# Attribute pool (apool) builder
# ---------------------------------------------------------------------------

class AttribPool:
    """Incremental apool used to mint attrib numbers as we build a changeset.

    Ordering matters for readability only — the server treats the pool as a
    set. We emit keys in insertion order (i.e. the order they first appear).
    """

    def __init__(self):
        self.num_to_attrib: dict[str, list[str]] = {}
        self.attrib_to_num: dict[str, int] = {}
        self.next_num: int = 0

    def intern(self, key: str, value: str) -> int:
        lookup = f'{key},{value}'
        if lookup in self.attrib_to_num:
            return self.attrib_to_num[lookup]
        n = self.next_num
        self.attrib_to_num[lookup] = n
        self.num_to_attrib[str(n)] = [key, value]
        self.next_num += 1
        return n

    def to_dict(self) -> dict:
        return {
            'nextNum': self.next_num,
            'numToAttrib': self.num_to_attrib,
            'attribToNum': self.attrib_to_num,
        }


# ---------------------------------------------------------------------------
# Styled run representation
# ---------------------------------------------------------------------------

def encode_link_value(url: str) -> str:
    """URL-encode exactly the characters Feishu does.

    Observation from samples: `https://example.com` → `https%3A%2F%2Fexample.com`
    — percent-encodes `:` and `/` but leaves alphanumerics. Python's
    `quote(url, safe='')` matches this pattern.
    """
    return quote(url, safe='')


def normalise_attrs(attrs: dict, author_uid: str) -> list[tuple[str, str]]:
    """Turn a style dict into a flat (key, value) list ready for apool.

    Always prepends `author` (required for collaboration provenance).
    Omits keys whose value is falsy *and* not explicitly empty-string.

    Special keys:
        `mention_doc`: dict {token, title, raw_url} → emitted as a single
            `inline-component` attribute whose value is the JSON string
            described in feishu_docx_easysync_format.md §4.11. The run's
            visible text must be a single space.
    """
    out: list[tuple[str, str]] = [('author', author_uid)]
    if not attrs:
        return out
    for k, v in attrs.items():
        if k == 'author':
            continue
        if k == 'mention_doc' and v:
            out.append(('inline-component', _encode_mention_doc(v)))
            continue
        if k == 'link' and v:
            out.append(('link', encode_link_value(v)))
            continue
        if isinstance(v, bool):
            out.append((k, 'true' if v else ''))
            continue
        if v is None:
            continue
        out.append((k, str(v)))
    return out


def _encode_mention_doc(data: dict) -> str:
    """Build the `inline-component` JSON string for a doc mention.

    Schema (from captured data): a JSON object serialised as a string. The
    server stores the value verbatim; JS client parses on render.
    """
    payload = {
        'id': str(_uuid.uuid4()),
        'type': 'mention_doc',
        'data': {
            'file_type': 22,
            'icon_type': 22,
            'token': data.get('token', ''),
            'raw_url': data.get('raw_url', ''),
            'title': data.get('title', ''),
        },
    }
    return _json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


# ---------------------------------------------------------------------------
# Changeset builder
# ---------------------------------------------------------------------------

def _emit_attr_ops(attr_nums: Sequence[int]) -> str:
    """Emit the `*N*M` opcodes for a set of attrib numbers.

    Convention (observed in captures): numbers are emitted in **descending**
    order of apool number. Semantically order-agnostic.
    """
    return ''.join(f'*{to_base36(n)}' for n in sorted(attr_nums, reverse=True))


def build_full_replace_changeset(
    old_text: str,
    runs: Sequence[tuple[str, dict]],
    author_uid: str,
) -> tuple[str, dict]:
    """Replace the entire text of a block with `runs` (styled segments).

    Produces `Z:<old>|<delta>|-<old>*A*B+<chunk>*C+<chunk>…$<all_text>` where
    each run contributes its own `*attr...+len` group.

    Args:
        old_text: the block's current text (empty string if the block is empty)
        runs: sequence of (text, attr_dict) pairs, in visual order
        author_uid: required for the `author` attribute on every run

    Returns:
        (changeset_string, apool_dict)
    """
    pool = AttribPool()
    old_len = utf16_length(old_text)
    new_len = sum(utf16_length(t) for t, _ in runs)
    delta = new_len - old_len

    parts: list[str] = [f'Z:{to_base36(old_len)}']
    parts.append(f'>{to_base36(delta)}' if delta >= 0 else f'<{to_base36(-delta)}')

    if old_len > 0:
        parts.append(f'-{to_base36(old_len)}')

    char_bank: list[str] = []
    for text, attrs in runs:
        if not text:
            continue
        attr_nums = [pool.intern(k, v) for k, v in normalise_attrs(attrs, author_uid)]
        parts.append(_emit_attr_ops(attr_nums))
        parts.append(f'+{to_base36(utf16_length(text))}')
        char_bank.append(text)

    parts.append('$')
    parts.append(''.join(char_bank))
    return ''.join(parts), pool.to_dict()


# ---------------------------------------------------------------------------
# Top-level payload helpers
# ---------------------------------------------------------------------------

def build_easysync_op(
    old_text: str,
    runs: Sequence[tuple[str, dict]],
    author_uid: str,
    zone: str = '0',
) -> dict:
    """Build a complete `subType: easysync` op ready to drop into a
    `user_change` payload at `p: ["text"]`."""
    cs, apool = build_full_replace_changeset(old_text, runs, author_uid)
    return {
        'p': ['text'],
        'subType': {
            't': 'easysync',
            'o': {
                'zone_changesets': {zone: cs},
                'apool': apool,
            },
        },
    }


def build_code_block_replace_op(
    old_text: str,
    new_text: str,
    author_uid: str,
    zone: str = '0',
) -> dict:
    """Build an easysync op that replaces a code block's text body.

    The text (old and new) is split at the LAST `\\n`:
      - prefix (newline-terminated portion) → `*A|N+M` (insert) or `|N-M` (delete)
      - tail (chars after the last `\\n`)   → `*A+M`   (insert) or `-M`   (delete)

    Bundling everything into one `|N*A+M` chunk triggers server-side
    `RepresentationCheck failed, lines mismatched`. Verified byte-exact
    against captured browser samples (see TestCodeBlockReplaceOp).

    Empty `new_text` is allowed (browser supports an empty code block).
    """
    pool = AttribPool()
    author_num = pool.intern('author', author_uid)
    author_b36 = to_base36(author_num)

    old_len = utf16_length(old_text)
    new_len = utf16_length(new_text)
    delta = new_len - old_len

    parts: list[str] = [f'Z:{to_base36(old_len)}']
    parts.append(f'>{to_base36(delta)}' if delta >= 0 else f'<{to_base36(-delta)}')

    if old_len > 0:
        last_nl = old_text.rfind('\n')
        if last_nl == -1:
            parts.append(f'-{to_base36(old_len)}')
        else:
            prefix = old_text[:last_nl + 1]
            tail = old_text[last_nl + 1:]
            parts.append(f'|{to_base36(prefix.count(chr(10)))}-{to_base36(utf16_length(prefix))}')
            if tail:
                parts.append(f'-{to_base36(utf16_length(tail))}')

    if new_len > 0:
        last_nl = new_text.rfind('\n')
        if last_nl == -1:
            parts.append(f'*{author_b36}+{to_base36(new_len)}')
        else:
            prefix = new_text[:last_nl + 1]
            tail = new_text[last_nl + 1:]
            parts.append(
                f'*{author_b36}|{to_base36(prefix.count(chr(10)))}+{to_base36(utf16_length(prefix))}'
            )
            if tail:
                parts.append(f'*{author_b36}+{to_base36(utf16_length(tail))}')

    parts.append('$')
    parts.append(new_text)

    return {
        'p': ['text'],
        'subType': {
            't': 'easysync',
            'o': {
                'zone_changesets': {zone: ''.join(parts)},
                'apool': pool.to_dict(),
            },
        },
    }
