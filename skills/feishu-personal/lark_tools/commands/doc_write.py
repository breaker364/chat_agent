"""
commands/doc_write.py - Docx cloud document write commands (Phase 1).

Creates and modifies Feishu docx cloud documents via:
- POST /space/api/wiki/v2/tree/create_node/  (create)
- POST /space/api/docx/blocks/user_change    (edit, via JSON OT)

Markdown is converted to OT ops that set `text.initialAttributedTexts` in full
(bypasses easysync changeset encoding — see docx_ot.py).
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Optional

from ..config import DOC_HOST
from ..http_utils import http_get, http_post_with_cookies
from ..docx_ot import (
    parse_markdown, build_insert_change_map, parse_inline_markdown, _new_block_id,
    _multiline_text_payload, _normalise_language,
)
from ..docx_upload import probe_image, upload_docx_image
from ..easysync import build_code_block_replace_op, build_easysync_op
from .doc import resolve_doc_token, fetch_doc_blocks, extract_block_text


# ---------------------------------------------------------------------------
# member_id + current user uid
# ---------------------------------------------------------------------------

def fetch_member_list(cookies, docx_token: str) -> dict:
    """Call /space/api/rce/member_list?obj_type=22&obj_token=<token>.

    Returns the `data` object: {members, entities, ...}.
    """
    url = f'/space/api/rce/member_list?obj_type=22&obj_token={docx_token}'
    res = http_get(cookies, DOC_HOST, url)
    body = res.get('data') or {}
    if body.get('code') != 0:
        raise RuntimeError(f'member_list failed: code={body.get("code")} msg={body.get("msg")}')
    return body.get('data') or {}


def get_current_user_uid(cookies) -> str:
    """Return the current user's uid from /space/api/user/."""
    res = http_get(cookies, DOC_HOST, '/space/api/user/')
    body = res.get('data') or {}
    if body.get('code') != 0:
        raise RuntimeError(f'/space/api/user failed: code={body.get("code")}')
    data = body.get('data') or {}
    uid = data.get('id') or data.get('suid') or data.get('user_id')
    if not uid:
        raise RuntimeError('Could not extract user_id from /space/api/user/')
    return str(uid)


def generate_member_id() -> str:
    """Generate a client-side member_id.

    The reference says "客户端生成" (client-generated) — the ID is just a
    per-session marker the server uses for optimistic concurrency + echo
    suppression. Any unique 14-digit-ish number works.
    """
    import random
    # 14 digits, first non-zero (matches observed patterns like "14334708804569")
    return str(random.randint(10**13, 10**14 - 1))


# ---------------------------------------------------------------------------
# Root block (page_id) info: version + current children count
# ---------------------------------------------------------------------------

def fetch_root_block_info(cookies, docx_token: str) -> dict:
    """Return {version, children_count} for the root (page) block."""
    result = fetch_doc_blocks(cookies, docx_token)
    if result.get('error'):
        raise RuntimeError(f'Failed to load doc: {result}')
    root = (result['blocks'].get(docx_token) or {})
    data = root.get('data') or {}
    # version lives on the block wrapper, not inside data
    version = root.get('version')
    if version is None:
        # some responses put it inside `data`
        version = data.get('version', 1)
    children = data.get('children') or []
    return {'version': int(version), 'children_count': len(children), 'block_data': data}


# ---------------------------------------------------------------------------
# user_change POST
# ---------------------------------------------------------------------------

def post_user_change(
    cookies,
    docx_token: str,
    member_id: str,
    change_map: dict,
    retry_on_conflict: bool = True,
) -> dict:
    """POST a user_change. On version conflict (-1101 / 1021), refetch versions
    and retry **once** with corrected versions."""
    payload = {
        'member_id': member_id,
        'uuid': str(uuid.uuid4()),
        'page_id': docx_token,
        'change_map': change_map,
    }
    res = http_post_with_cookies(
        cookies, DOC_HOST, '/space/api/docx/blocks/user_change', payload,
    )
    body = res.get('data') or {}
    code = body.get('code')
    if code == 0:
        return body

    # Version conflict: refetch root version and retry.
    if retry_on_conflict and code in (-1101, 1021):
        print(f'[doc] user_change conflict (code={code}), refetching versions...', file=sys.stderr)
        info = fetch_root_block_info(cookies, docx_token)
        # Only the parent (root) block has a version that might be stale.
        if docx_token in change_map:
            change_map[docx_token]['version'] = info['version']
        return post_user_change(cookies, docx_token, member_id, change_map, retry_on_conflict=False)

    raise RuntimeError(f'user_change failed: code={code} msg={body.get("msg")} body={body}')


# ---------------------------------------------------------------------------
# Public helpers used by commands
# ---------------------------------------------------------------------------

def _lookup_space_id(cookies, wiki_token: str) -> str:
    """Resolve the space_id that owns a given wiki node (for create_node子目录场景)."""
    res = http_get(
        cookies, DOC_HOST,
        f'/space/api/wiki/v2/tree/get_node/?wiki_token={wiki_token}&expand_shortcut=true',
    )
    body = res.get('data') or {}
    if body.get('code') != 0:
        raise RuntimeError(
            f'get_node failed for parent {wiki_token}: code={body.get("code")} msg={body.get("msg")}'
        )
    node = body.get('data') or {}
    space_id = node.get('space_id')
    if not space_id:
        raise RuntimeError(f'parent wiki node {wiki_token} has no space_id')
    return space_id


def create_empty_doc(cookies, title: str = '', parent_wiki_token: Optional[str] = None) -> dict:
    """Create a new docx in the user's wiki.

    If `parent_wiki_token` is given, the new node is placed as a child of that
    node (looking up its space_id first). Otherwise the doc lands at the root
    of the personal wiki.

    Returns the `data` block from create_node: {wiki_token, obj_token, url, ...}.
    """
    if parent_wiki_token:
        # Child-creation payload (verified 2026-05-06 against wiki/v2/tree/create_node)
        payload = {
            'space_id': _lookup_space_id(cookies, parent_wiki_token),
            'parent_wiki_token': parent_wiki_token,
            'ua_type': 'Web',
            'scene': 'wiki_create',
            'obj_type': 22,
            'node_type': 0,
            'synergy_uuid': str(int(time.time() * 1000)),
            'template_token': '',
        }
        if title:
            payload['title'] = title  # capture sample omits this; server accepts both
    else:
        # Root-creation payload (legacy capture; works for personal wiki root)
        payload = {
            'title': title or '',
            'ua_type': 'Web',
            'scene': 'wiki_create',
            'node_type': 0,
            'obj_type': 22,
        }
    res = http_post_with_cookies(
        cookies, DOC_HOST, '/space/api/wiki/v2/tree/create_node/', payload,
    )
    body = res.get('data') or {}
    if body.get('code') != 0:
        raise RuntimeError(f'create_node failed: code={body.get("code")} msg={body.get("msg")} body={body}')
    return body.get('data') or {}


def append_markdown(cookies, docx_token: str, markdown: str) -> dict:
    """Append markdown content to the end of a doc. Returns {blocks_added, block_ids}."""
    blocks = parse_markdown(markdown)
    if not blocks:
        return {'blocks_added': 0, 'block_ids': []}

    uid = get_current_user_uid(cookies)
    member_id = generate_member_id()
    info = fetch_root_block_info(cookies, docx_token)

    change_map, new_ids = build_insert_change_map(
        blocks=blocks,
        parent_id=docx_token,
        parent_version=info['version'],
        author_uid=uid,
        insert_at=info['children_count'],
    )
    resp = post_user_change(cookies, docx_token, member_id, change_map)
    return {
        'blocks_added': len(new_ids),
        'block_ids': new_ids,
        'response_type': (resp.get('data') or {}).get('type'),
    }


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------

def _read_input(text_arg: Optional[str], md_file: Optional[str], from_stdin: bool) -> str:
    if text_arg is not None:
        return text_arg
    if md_file:
        with open(md_file, 'r', encoding='utf-8') as f:
            return f.read()
    if from_stdin:
        return sys.stdin.read()
    return ''


def cmd_doc_create(cookies, title: str, text: Optional[str] = None, md_file: Optional[str] = None,
                   from_stdin: bool = False, parent_wiki_token: Optional[str] = None):
    """Create a new docx. Optionally write initial content from --text / --md-file / stdin."""
    node = create_empty_doc(cookies, title, parent_wiki_token=parent_wiki_token)
    obj_token = node.get('obj_token')
    wiki_token = node.get('wiki_token')
    url = node.get('url') or f'https://{DOC_HOST}/wiki/{wiki_token}'

    content = _read_input(text, md_file, from_stdin)
    result = {
        'success': True,
        'title': title,
        'obj_token': obj_token,
        'wiki_token': wiki_token,
        'url': url,
    }
    if content.strip():
        # small delay so the server finishes initializing the empty root block
        time.sleep(0.5)
        ap = append_markdown(cookies, obj_token, content)
        result['blocks_added'] = ap['blocks_added']
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_doc_append(cookies, token_or_url: str, text: Optional[str] = None, md_file: Optional[str] = None, from_stdin: bool = False):
    docx_token = resolve_doc_token(cookies, token_or_url)
    content = _read_input(text, md_file, from_stdin)
    if not content.strip():
        print(json.dumps({'error': 'No content to append (use --text, --md-file, or --stdin)'}))
        return
    ap = append_markdown(cookies, docx_token, content)
    print(json.dumps({
        'success': True,
        'obj_token': docx_token,
        'blocks_added': ap['blocks_added'],
    }, indent=2, ensure_ascii=False))


def _set_root_block_title(cookies, docx_token: str, new_title: str) -> dict:
    """Overwrite the root page block's text (== in-doc title) via user_change.

    The root block's text is stored like any other block, so we emit an
    `oi` action that replaces `text.initialAttributedTexts` in-place.
    """
    from ..docx_ot import _text_payload
    uid = get_current_user_uid(cookies)
    member_id = generate_member_id()
    info = fetch_root_block_info(cookies, docx_token)
    payload = _text_payload(new_title, uid)

    change_map = {
        docx_token: {
            'id': docx_token,
            'version': info['version'],
            'payload': {
                'ops': [{
                    'p': ['text'],
                    'action': {'oi': payload},
                }],
            },
        }
    }
    return post_user_change(cookies, docx_token, member_id, change_map)


def cmd_doc_set_title(cookies, token_or_url: str, new_title: str):
    """Rename a docx.

    Feishu shows titles in two places:
      - Wiki tree breadcrumb → /space/api/wiki/v2/tree/update_title/
      - In-doc title (top of page, === root block text) → user_change on root

    We update both so the result stays consistent.
    """
    import re
    wiki_token = None
    m = re.search(r'/wiki/([A-Za-z0-9]+)', token_or_url)
    if m:
        wiki_token = m.group(1)

    docx_token = resolve_doc_token(cookies, token_or_url)
    result = {'success': True, 'obj_token': docx_token, 'title': new_title}

    if wiki_token:
        res = http_post_with_cookies(
            cookies, DOC_HOST, '/space/api/wiki/v2/tree/update_title/',
            {'wiki_token': wiki_token, 'title': new_title},
        )
        body = res.get('data') or {}
        result['wiki_token'] = wiki_token
        result['wiki_updated'] = body.get('code') == 0
        if body.get('code') != 0:
            result['wiki_error'] = body

    # Always update in-doc title (the root block's text).
    try:
        _set_root_block_title(cookies, docx_token, new_title)
        result['in_doc_updated'] = True
    except Exception as e:
        result['in_doc_updated'] = False
        result['in_doc_error'] = str(e)
        result['success'] = False

    print(json.dumps(result, indent=2, ensure_ascii=False))


def fetch_block_by_id(cookies, docx_token: str, block_id: str) -> dict:
    """Return the {id, version, data} entry for a block, or None if missing.

    Loads all blocks via client_vars and filters — not the most efficient path
    but straightforward and aligns with how `doc read` already works.
    """
    result = fetch_doc_blocks(cookies, docx_token)
    if result.get('error'):
        raise RuntimeError(f'Failed to load doc: {result}')
    entry = result['blocks'].get(block_id)
    if not entry:
        return None
    return entry


def cmd_doc_edit(cookies, token_or_url: str, block_id: str, replace_text: Optional[str]):
    """Replace the full text of a single block, preserving rich formatting via easysync.

    The new text is parsed for inline markdown (**bold** / *italic* / [link](url)
    / `code` / ~~strike~~), then encoded as a changeset that deletes the block's
    entire old text and re-inserts styled runs.

    Only single-line text/heading/bullet/ordered/quote/code blocks are supported.
    """
    if replace_text is None:
        raise ValueError('cmd_doc_edit requires replace_text')

    docx_token = resolve_doc_token(cookies, token_or_url)
    entry = fetch_block_by_id(cookies, docx_token, block_id)
    if entry is None:
        print(json.dumps({'error': f'block {block_id} not found in doc {docx_token}'}))
        return

    block_data = entry.get('data') or {}
    block_type = block_data.get('type', '')
    if block_type in ('divider', 'image', 'sheet', 'bitable'):
        print(json.dumps({'error': f'block type {block_type!r} has no editable text'}))
        return

    old_text = extract_block_text(block_data)
    uid = get_current_user_uid(cookies)
    member_id = generate_member_id()

    # Parse the new text as inline markdown → styled runs.
    runs = parse_inline_markdown(replace_text)
    easysync_op = build_easysync_op(old_text, runs, uid)

    change_map = {
        block_id: {
            'id': block_id,
            'version': int(entry.get('version') or 1),
            'payload': {
                'ops': [easysync_op],
            },
        }
    }
    resp = post_user_change(cookies, docx_token, member_id, change_map)
    print(json.dumps({
        'success': True,
        'obj_token': docx_token,
        'block_id': block_id,
        'old_text': old_text,
        'new_text': ''.join(t for t, _ in runs),
        'response_type': (resp.get('data') or {}).get('type'),
    }, indent=2, ensure_ascii=False))


def _image_oi(parent_id: str, author_uid: str, image: dict, file_token: str = '') -> dict:
    """Build the `image` block OI. Shape matches the captured schema in
    feishu_docx_block_schemas.md §8."""
    return {
        'parent_id': parent_id,
        'type': 'image',
        'author': author_uid,
        'children': [],
        'comments': [],
        'revisions': [],
        'area_comments': {},
        'align': 'center',
        'image': {
            'src': '',
            'token': file_token,
            'mimeType': image['mime'],
            'name': image['name'],
            'size': image['size'],
            'width': image['width'],
            'height': image['height'],
            'scale': 1,
        },
    }


def insert_image(cookies, docx_token: str, image_path: str) -> dict:
    """Insert a local image at the end of a doc. Returns {block_id, file_token}.

    Three phases:
      1. user_change → create placeholder image block (token=""), attach to root
      2. 4-step upload → get file_token
      3. user_change → replace placeholder image.token with real file_token
    """
    image = probe_image(image_path)

    uid = get_current_user_uid(cookies)
    member_id = generate_member_id()
    info = fetch_root_block_info(cookies, docx_token)

    # 1. placeholder
    block_id = _new_block_id()
    placeholder_oi = _image_oi(docx_token, uid, image, file_token='')
    placeholder_map = {
        block_id: {
            'id': block_id,
            'version': 0,
            'payload': {'ops': [{'p': [], 'action': {'oi': placeholder_oi}}]},
        },
        docx_token: {
            'id': docx_token,
            'version': info['version'],
            'payload': {
                'ops': [{'p': ['children', info['children_count']], 'action': {'li': block_id}}],
            },
        },
    }
    post_user_change(cookies, docx_token, member_id, placeholder_map)

    # 2. upload
    file_token = upload_docx_image(cookies, docx_token, block_id, image)

    # 3. backfill token — replace the entire `image` sub-object
    old_image = dict(placeholder_oi['image'])
    new_image = dict(old_image)
    new_image['token'] = file_token
    backfill_map = {
        block_id: {
            'id': block_id,
            'version': 1,  # placeholder created it at version 1 (post-create)
            'payload': {
                'ops': [{
                    'p': ['image'],
                    'action': {'od': old_image, 'oi': new_image},
                }],
            },
        }
    }
    post_user_change(cookies, docx_token, member_id, backfill_map)
    return {'block_id': block_id, 'file_token': file_token, 'image': image}


def cmd_doc_insert_image(cookies, token_or_url: str, image_path: str):
    """CLI: append a local image to the end of a docx."""
    if not os.path.isfile(image_path):
        print(json.dumps({'error': f'image not found: {image_path}'}))
        return
    docx_token = resolve_doc_token(cookies, token_or_url)
    result = insert_image(cookies, docx_token, image_path)
    print(json.dumps({
        'success': True,
        'obj_token': docx_token,
        'block_id': result['block_id'],
        'file_token': result['file_token'],
        'width': result['image']['width'],
        'height': result['image']['height'],
        'size': result['image']['size'],
    }, indent=2, ensure_ascii=False))


def cmd_doc_edit_code(
    cookies,
    token_or_url: str,
    block_id: str,
    new_language: Optional[str] = None,
    new_content: Optional[str] = None,
    content_file: Optional[str] = None,
):
    """Update a code block's `language` and/or body. At least one of
    `--language` or `--content` / `--content-file` must be set."""
    if new_content is None and content_file:
        with open(content_file, 'r', encoding='utf-8') as f:
            new_content = f.read()
    if new_language is None and new_content is None:
        print(json.dumps({'error': 'provide --language and/or --content / --content-file'}))
        return

    docx_token = resolve_doc_token(cookies, token_or_url)
    entry = fetch_block_by_id(cookies, docx_token, block_id)
    if entry is None:
        print(json.dumps({'error': f'block {block_id} not found'}))
        return
    block_data = entry.get('data') or {}
    if block_data.get('type') != 'code':
        print(json.dumps({
            'error': f'block is type={block_data.get("type")!r}, not `code` — use doc edit for text blocks',
        }))
        return

    uid = get_current_user_uid(cookies)
    member_id = generate_member_id()
    ops = []

    if new_language is not None:
        norm_lang = _normalise_language(new_language)
        old_lang = block_data.get('language') or 'Plain Text'
        ops.append({'p': ['language'], 'action': {'od': old_lang, 'oi': norm_lang}})
        # is_language_picked: send op only if the field's current value differs
        # from True. `oi` alone is valid only when the field is absent — if it
        # already exists, the op must carry `od`, otherwise the server accepts
        # it but the OT history goes inconsistent and the browser refuses to
        # re-edit the block ("无法修改"). See references/feishu_docx_block_schemas.md §4.2.
        old_picked = block_data.get('is_language_picked')
        if old_picked is None:
            ops.append({'p': ['is_language_picked'], 'action': {'oi': True}})
        elif old_picked is not True:
            ops.append({
                'p': ['is_language_picked'],
                'action': {'od': old_picked, 'oi': True},
            })
        # else: already True — no-op

    if new_content is not None:
        old_text = (
            ((block_data.get('text') or {}).get('initialAttributedTexts') or {})
            .get('text', {})
            .get('0', '')
        )
        ops.append(build_code_block_replace_op(old_text, new_content, uid))

    change_map = {
        block_id: {
            'id': block_id,
            'version': int(entry.get('version') or 1),
            'payload': {'ops': ops},
        }
    }
    resp = post_user_change(cookies, docx_token, member_id, change_map)
    print(json.dumps({
        'success': True,
        'obj_token': docx_token,
        'block_id': block_id,
        'language': _normalise_language(new_language) if new_language else block_data.get('language'),
        'content_updated': new_content is not None,
        'response_type': (resp.get('data') or {}).get('type'),
    }, indent=2, ensure_ascii=False))


def cmd_doc_delete_block(cookies, token_or_url: str, block_id: str):
    """Detach a block from the parent doc (the block record is kept but hidden)."""
    docx_token = resolve_doc_token(cookies, token_or_url)
    info = fetch_root_block_info(cookies, docx_token)
    children = (info['block_data'].get('children') or [])
    if block_id not in children:
        print(json.dumps({
            'error': f'block_id {block_id} not in root children',
            'children': children,
        }, indent=2))
        return
    idx = children.index(block_id)

    uid = get_current_user_uid(cookies)
    member_id = generate_member_id()

    change_map = {
        docx_token: {
            'id': docx_token,
            'version': info['version'],
            'payload': {
                'ops': [{'p': ['children', idx], 'action': {'ld': block_id}}],
            },
        }
    }
    resp = post_user_change(cookies, docx_token, member_id, change_map)
    print(json.dumps({
        'success': True,
        'obj_token': docx_token,
        'removed_block_id': block_id,
        'response_type': (resp.get('data') or {}).get('type'),
    }, indent=2, ensure_ascii=False))
