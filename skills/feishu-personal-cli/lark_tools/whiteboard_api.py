"""
whiteboard_api.py - Protocol helpers for Feishu whiteboard (画板).

Whiteboards are stored as blocks inside a docx (obj_type=22). A whiteboard has
two distinct tokens:
  - blockToken: 25-char base62, the whiteboard's own server-side data id (e.g. Pf17wUhHkh3iEsbv1XxcEjgYnZg)
  - block_id:   27-char base62, the docx-side block id (e.g. M2kUd6BwQoRmaHxWAP2cITVQnOc)

Read path is pure REST; no OT, no ws_ticket required.
"""

import re
import sys

from .config import DOC_HOST
from .http_utils import http_get, http_get_binary


def is_blocktoken(s: str) -> bool:
    """Heuristic: a raw whiteboard blockToken is a single alphanumeric token (no slashes)."""
    return bool(s) and '/' not in s and '://' not in s and bool(re.fullmatch(r'[A-Za-z0-9]+', s))


def fetch_whiteboard_block(cookies, block_token: str) -> dict:
    """GET /space/api/whiteboard/block — full meta + nodes JSON.

    Returns the inner `data` dict on success, or {'error': ..., 'response': ...} on failure.
    """
    path = f'/space/api/whiteboard/block?blockToken={block_token}&reqVersion=1&clientVersion=12.4'
    res = http_get(cookies, DOC_HOST, path)
    body = res.get('data') or {}
    if body.get('code') != 0:
        return {'error': 'Failed to fetch whiteboard', 'response': body}
    return body.get('data') or {}


def download_whiteboard_png(cookies, block_token: str) -> dict:
    """GET /space/api/file/f/cdp-whiteboard-<bt>~noop/ — server-rendered PNG.

    Returns {status, buffer (bytes), contentType}.
    """
    url = f'https://{DOC_HOST}/space/api/file/f/cdp-whiteboard-{block_token}~noop/'
    return http_get_binary(cookies, url)


def list_whiteboards_in_docx(cookies, docx_token: str) -> list:
    """Walk the docx block tree, return whiteboard blocks as a list of dicts:
        [{block_id, block_token, parent_id}, ...]

    Uses fetch_doc_blocks from commands.doc (already paginates).
    """
    # Imported lazily to avoid a circular import with commands.doc
    from .commands.doc import fetch_doc_blocks

    result = fetch_doc_blocks(cookies, docx_token)
    if result.get('error'):
        return []
    blocks = result.get('blocks') or {}
    out = []
    for block_id, entry in blocks.items():
        data = (entry or {}).get('data') or {}
        if data.get('type') == 'whiteboard':
            out.append({
                'block_id': block_id,
                'block_token': data.get('token', ''),
                'parent_id': data.get('parent_id', ''),
            })
    return out


def resolve_block_token(cookies, token_or_url: str, block_id_flag: str = None) -> dict:
    """Resolve user input into a blockToken.

    Accepts:
      - Raw blockToken (no slash) → returned as-is.
      - URL (wiki/docx) + explicit --block-id → use the provided block_id_flag as blockToken.
      - URL (wiki/docx) alone → list whiteboards in the docx:
          * exactly one → use it.
          * zero → error.
          * >1 → error with the list, asking user to specify --block-id.

    Returns {block_token, docx_token?, candidates?, error?}.
    """
    # Case 1: raw blockToken
    if is_blocktoken(token_or_url):
        return {'block_token': token_or_url}

    # Case 2: URL provided; resolve docx_token
    from .commands.doc import resolve_doc_token
    docx_token = resolve_doc_token(cookies, token_or_url)

    # Explicit --block-id flag wins
    if block_id_flag and is_blocktoken(block_id_flag):
        return {'block_token': block_id_flag, 'docx_token': docx_token}

    # Otherwise, auto-discover whiteboards in this docx
    candidates = list_whiteboards_in_docx(cookies, docx_token)
    if len(candidates) == 1:
        return {'block_token': candidates[0]['block_token'], 'docx_token': docx_token}
    if len(candidates) == 0:
        return {
            'error': 'no whiteboard block found in this docx',
            'docx_token': docx_token,
        }
    return {
        'error': 'multiple whiteboards in this docx; pass --block-id <token>',
        'docx_token': docx_token,
        'candidates': candidates,
    }


def find_syntax_containers(nodes: list) -> list:
    """Walk a nodes tree (from fetch_whiteboard_block) and yield syntax-source containers.

    Returns a list of dicts: [{id, syntax_type (1=PlantUML, 2=Mermaid), source_code, diagram_type}].
    Recurses into `children`.
    """
    out = []

    def walk(node_list):
        for n in node_list or []:
            info = (n or {}).get('info') or {}
            sp = info.get('syntaxProps') or {}
            src = sp.get('sourceCode')
            if src:
                out.append({
                    'id': n.get('id', ''),
                    'syntax_type': sp.get('syntaxType'),
                    'diagram_type': sp.get('diagramType'),
                    'source_code': src,
                })
            children = n.get('children') or []
            if children:
                walk(children)

    walk(nodes)
    return out
