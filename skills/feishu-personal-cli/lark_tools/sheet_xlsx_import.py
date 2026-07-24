"""
sheet_xlsx_import.py — xlsx → 飞书电子表格 5-step import flow.

Reverse-engineered 2026-05-18 from a drive-upload sniff. The browser version
of "上传 xlsx → 自动建飞书 sheet" does this:

  1. POST /space/api/box/upload/prepare/                  → upload_id
  2. POST /space/api/box/stream/upload/merge_block/       → xlsx bytes
  3. POST /space/api/box/upload/finish/                   → file_token (xlsx)
  4. POST /space/api/import/create/                       → ticket
  5. GET  /space/api/import/result/<ticket>               → poll until job_status=0
                                                            → final sheet token + URL

Differences vs docx image upload (lark_tools/docx_upload.py):
  - mount_point = 'ccm_import' (not 'docx_image')
  - prepare/finish run on DOC_HOST (nio.feishu.cn), not UPLOAD_HOST
  - 'blocks/' step is skipped (server doesn't require pre-declared hash for ccm_import)
  - Extra import/create + import/result step pair triggers the xlsx→sheet conversion
    and returns the final wiki token (sheet lives behind a wiki node, same as
    `lark sheet create` empty-sheet flow).

Supports both single-block (≤ block_size, usually 4MB) and multi-block
uploads. Multi-block flow adds a `/box/upload/blocks/` declaration step
and loops merge_block once per chunk; verified 2026-05-18 against a
17.64MB xlsx capture.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
import zlib

from .config import DOC_HOST, DOC_IMAGE_HOST
from .http_utils import http_get, http_post_with_cookies, http_post_binary


_DEFAULT_BLOCK_SIZE = 4 * 1024 * 1024  # server typically returns 4MB
_POLL_INTERVAL_S = 1.0
_POLL_TIMEOUT_S = 60.0


def _sha256_b64(data: bytes) -> str:
    """Per-block hash used in /box/upload/blocks/ declarations (also enables
    server-side dedupe — if hash matches an existing block the server
    answers without listing it in needed_upload_blocks)."""
    return base64.b64encode(hashlib.sha256(data).digest()).decode('ascii')


def _crc32_str(data: bytes) -> str:
    """Per-block checksum used in /box/upload/blocks/ declarations."""
    return str(zlib.crc32(data) & 0xFFFFFFFF)


def _adler32_str(data: bytes) -> str:
    """x-block-list-checksum header is Adler-32 (verified in docx_upload.py against
    a real merge_block capture). Same applies to the ccm_import path."""
    return str(zlib.adler32(data) & 0xFFFFFFFF)


def _post(cookies, host: str, path: str, body: dict) -> dict:
    res = http_post_with_cookies(cookies, host, path, body)
    payload = (res.get('data') or {})
    if payload.get('code') != 0:
        raise RuntimeError(
            f'POST {path} failed: code={payload.get("code")} '
            f'msg={payload.get("message") or payload.get("msg")} body={payload}'
        )
    return payload.get('data') or {}


def import_xlsx_to_sheet(
    cookies,
    xlsx_path: str,
    parent_wiki_token: str,
    title: str | None = None,
) -> dict:
    """Upload a local .xlsx file and have the server convert it to a Feishu
    spreadsheet under the given wiki parent.

    Returns:
        {
            'wikiToken':         <final wiki node token>,
            'url':               <https://nio.feishu.cn/wiki/...>,
            'spreadsheetToken':  <obj_token resolved from wiki>,
            'sheetsUrl':         <https://nio.feishu.cn/sheets/...>,
            'title':             <basename without .xlsx>,
        }
    """
    if not os.path.isfile(xlsx_path):
        raise FileNotFoundError(xlsx_path)
    if not parent_wiki_token:
        raise ValueError('parent_wiki_token is required (drive needs a destination)')

    name = os.path.basename(xlsx_path)
    if not name.lower().endswith('.xlsx'):
        raise ValueError(f'expected .xlsx file, got: {name}')
    with open(xlsx_path, 'rb') as f:
        data = f.read()
    size = len(data)

    extra_json = json.dumps(
        {'obj_type': 'sheet', 'file_extension': 'xlsx'},
        ensure_ascii=False, separators=(',', ':'),
    )

    # 1. prepare — server tells us block_size + num_blocks
    prep = _post(cookies, DOC_HOST, '/space/api/box/upload/prepare/?shouldBypassScsDialog=true', {
        'mount_point': 'ccm_import',
        'mount_node_token': parent_wiki_token,
        'name': name,
        'size': size,
        'extra': {'extra': extra_json},
        'size_checker': True,
    })
    upload_id = prep['upload_id']
    block_size = prep.get('block_size', _DEFAULT_BLOCK_SIZE)
    num_blocks = prep.get('num_blocks') or max(1, (size + block_size - 1) // block_size)

    # 2. Upload bytes. Branch on size: single-block (1 merge_block, no blocks/
    #    pre-declare needed) vs multi-block (blocks/ + merge_block per chunk).
    if num_blocks <= 1:
        _upload_single_block(cookies, data, upload_id, block_size)
    else:
        _upload_multi_block(cookies, data, upload_id, block_size, num_blocks)

    # 3. finish — returns the xlsx file_token (NOT the sheet token yet)
    fin = _post(cookies, DOC_HOST, '/space/api/box/upload/finish/?shouldBypassScsDialog=true', {
        'upload_id': upload_id,
        'num_blocks': num_blocks,
        'mount_point': 'ccm_import',
        'push_open_history_record': 0,
    })
    file_token = fin.get('file_token')
    if not file_token:
        raise RuntimeError(f'finish returned no file_token: {fin}')

    # 4. import/create — triggers xlsx→sheet conversion, returns ticket
    create = _post(cookies, DOC_HOST, '/space/api/import/create/', {
        'file_token': file_token,
        'type': 'sheet',
        'file_extension': 'xlsx',
        # mount_type=2 observed in capture (likely "wiki space"; enum unverified).
        # mount_key mirrors prepare's mount_node_token.
        'point': {'mount_type': 2, 'mount_key': parent_wiki_token},
        'event_source': '1',
        'client_token': str(uuid.uuid4()),
        # passback echoes back to client; minimal value works in capture.
        'passback': json.dumps({'time_zone': 'Asia/Shanghai'}, separators=(',', ':')),
    })
    ticket = create.get('ticket')
    if not ticket:
        raise RuntimeError(f'import/create returned no ticket: {create}')

    # 5. import/result — poll until job_status=0 (success) or timeout
    deadline = time.time() + _POLL_TIMEOUT_S
    last = None
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL_S)
        result = _poll_import_result(cookies, ticket)
        last = result
        status = result.get('job_status')
        if status == 0:  # success
            wiki_token = result.get('token')
            url = result.get('url')
            if not wiki_token:
                raise RuntimeError(f'import/result success but no token: {result}')
            # Resolve wiki → sheet obj_token (also surfaces space_id for teardown).
            sheet_token, sheets_url, space_id = _resolve_wiki_to_sheet(cookies, wiki_token)
            return {
                'wikiToken': wiki_token,
                'url': url,
                'spreadsheetToken': sheet_token,
                'sheetsUrl': sheets_url,
                'spaceId': space_id,
                'title': title or os.path.splitext(name)[0],
            }
        if status not in (None, 2):  # 2 = in-progress; other non-zero values likely mean error
            raise RuntimeError(f'import/result failed: status={status} body={result}')

    raise TimeoutError(f'import/result polling timed out after {_POLL_TIMEOUT_S}s; last={last}')


def _upload_single_block(cookies, data: bytes, upload_id: str, block_size: int) -> None:
    """Send the whole file as one merge_block call. Used when prepare
    reported num_blocks=1."""
    headers = {
        'x-seq-list': '0',
        'x-block-list-checksum': _adler32_str(data),
        'x-block-origin-size': str(block_size),
    }
    merge_path = (
        f'/space/api/box/stream/upload/merge_block/'
        f'?shouldBypassScsDialog=true&upload_id={upload_id}&mount_point=ccm_import'
    )
    mb_res = http_post_binary(cookies, DOC_IMAGE_HOST, merge_path, data, headers)
    mb_body = (mb_res.get('data') or {})
    if mb_body.get('code') != 0:
        raise RuntimeError(f'merge_block (single) failed: {mb_body}')


def _upload_multi_block(
    cookies, data: bytes, upload_id: str, block_size: int, num_blocks: int,
) -> None:
    """Multi-block path: pre-declare per-block hash+checksum via /blocks/,
    then send each chunk through merge_block one by one.

    The browser batches multiple seq numbers per merge_block call, but the
    server also accepts one-seq-per-call (verified by sending single-block
    uploads with the same shape). We pick the simpler one-per-call form.
    """
    # Slice into chunks first so we can hash + checksum each.
    chunks = []
    for i in range(num_blocks):
        start = i * block_size
        chunks.append(data[start:start + block_size])

    # /blocks/ — pre-declare. `isUploaded: True` matches the browser capture
    # (looks like a dedupe hint; server-side response still lists everything
    # in needed_upload_blocks unless a hash truly matches an existing block).
    blocks_payload = [
        {
            'hash': _sha256_b64(chunk),
            'seq': i,
            'size': len(chunk),
            'checksum': _crc32_str(chunk),
            'isUploaded': True,
        }
        for i, chunk in enumerate(chunks)
    ]
    blocks_resp = _post(
        cookies, DOC_HOST, '/space/api/box/upload/blocks/?shouldBypassScsDialog=true',
        {'blocks': blocks_payload, 'upload_id': upload_id, 'mount_point': 'ccm_import'},
    )
    # Server returns the subset of blocks it actually wants uploaded. Honor
    # that to support dedupe (skip blocks the server already has).
    needed_seqs = {b.get('seq') for b in (blocks_resp.get('needed_upload_blocks') or [])}
    if not needed_seqs:
        # Defensive: if server returns nothing-needed, still send everything.
        needed_seqs = set(range(num_blocks))

    # merge_block per chunk
    merge_path_tpl = (
        '/space/api/box/stream/upload/merge_block/'
        '?shouldBypassScsDialog=true&upload_id={uid}&mount_point=ccm_import'
    )
    for i, chunk in enumerate(chunks):
        if i not in needed_seqs:
            continue
        headers = {
            'x-seq-list': str(i),
            'x-block-list-checksum': _adler32_str(chunk),
            'x-block-origin-size': str(block_size),
        }
        mb_res = http_post_binary(
            cookies, DOC_IMAGE_HOST,
            merge_path_tpl.format(uid=upload_id), chunk, headers,
        )
        mb_body = (mb_res.get('data') or {})
        if mb_body.get('code') != 0:
            raise RuntimeError(f'merge_block seq={i} failed: {mb_body}')


def _poll_import_result(cookies, ticket: str) -> dict:
    res = http_get(cookies, DOC_HOST, f'/space/api/import/result/{ticket}')
    body = res.get('data') or {}
    if body.get('code') != 0:
        raise RuntimeError(f'import/result query failed: {body}')
    return (body.get('data') or {}).get('result') or {}


def _resolve_wiki_to_sheet(cookies, wiki_token: str) -> tuple[str, str, str]:
    """Translate the wiki node token returned by import/result into:
    (sheet obj_token, direct /sheets/ URL, space_id).

    space_id is needed by wiki_delete.delete_wiki_node for test teardown.
    """
    res = http_get(
        cookies, DOC_HOST,
        f'/space/api/wiki/v2/tree/get_node/?wiki_token={wiki_token}&expand_shortcut=true',
    )
    node_data = (res.get('data') or {}).get('data') or {}
    obj_token = node_data.get('obj_token')
    space_id = node_data.get('space_id') or ''
    if not obj_token:
        # Fall back to the wiki token so callers at least get a usable URL.
        return wiki_token, f'https://{DOC_HOST}/wiki/{wiki_token}', space_id
    return obj_token, f'https://{DOC_HOST}/sheets/{obj_token}', space_id
