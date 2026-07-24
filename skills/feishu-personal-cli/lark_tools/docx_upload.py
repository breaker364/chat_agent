"""
docx_upload.py - docx image upload (4-step flow).

Implements the reversed-engineered sequence from
feishu_docx_image_upload.md:

    1. POST /space/api/box/upload/prepare/   (UPLOAD_HOST)
    2. POST /space/api/box/upload/blocks/    (UPLOAD_HOST, declare hashes)
    3. POST /space/api/box/stream/upload/merge_block/   (DOC_IMAGE_HOST, bytes)
    4. POST /space/api/box/upload/finish/    (UPLOAD_HOST → file_token)

Single-shot uploads only for now — multi-part (files > 4MB) is noted as
"untested" in the reference and deferred.
"""

from __future__ import annotations

import base64
import hashlib
import os
import zlib
from typing import Optional

from PIL import Image

from .config import DOC_IMAGE_HOST, UPLOAD_HOST
from .http_utils import http_post_with_cookies, http_post_binary


# Guard: the merge_block header x-block-origin-size must equal prepare's
# block_size (4MB per reference). If files grow beyond this, split into parts.
_MAX_SINGLE_BLOCK = 4 * 1024 * 1024


def _sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode('ascii')


def _crc32_str(data: bytes) -> str:
    return str(zlib.crc32(data) & 0xFFFFFFFF)


def _adler32_str(data: bytes) -> str:
    """x-block-list-checksum is Adler-32 (not CRC32 as the reference doc
    suggests). Verified against merge_block capture 2026-04-17T08-05-13_11:
    body adler32 = 3651481158, matching the captured header value."""
    return str(zlib.adler32(data) & 0xFFFFFFFF)


_MIME_BY_EXT = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
}


def probe_image(path: str) -> dict:
    """Return {bytes, size, width, height, mime, name} for a local image.

    Dimensions come from Pillow. Unknown extensions fall back to mime from
    PIL's format map (uppercased PNG / JPEG / …).
    """
    with open(path, 'rb') as f:
        data = f.read()
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    mime = _MIME_BY_EXT.get(ext)
    with Image.open(path) as im:
        w, h = im.size
        if not mime and im.format:
            mime = f'image/{im.format.lower()}'
    return {
        'bytes': data,
        'size': len(data),
        'width': w,
        'height': h,
        'mime': mime or 'application/octet-stream',
        'name': name,
    }


def upload_docx_image(
    cookies,
    docx_token: str,
    block_id: str,
    image: dict,
) -> str:
    """Run the 4-step docx image upload and return the final file_token.

    `image` is the dict returned by `probe_image`. Caller must have already
    inserted a placeholder image block via user_change and passes its block_id
    as `block_id` (used as mount_node_token in prepare).
    """
    if image['size'] > _MAX_SINGLE_BLOCK:
        raise NotImplementedError(
            f'multi-block uploads are not implemented (file size {image["size"]} > '
            f'{_MAX_SINGLE_BLOCK}); reference marks this path as untested.'
        )

    data = image['bytes']

    # 1. prepare
    prep = _post(cookies, UPLOAD_HOST, '/space/api/box/upload/prepare/', {
        'mount_point': 'docx_image',
        'mount_node_token': block_id,
        'name': image['name'],
        'size': image['size'],
        'extra': {'drive_route_token': docx_token},
        'size_checker': False,
    })
    upload_id = prep['upload_id']
    block_size = prep.get('block_size', _MAX_SINGLE_BLOCK)

    # 2. blocks — declare one block worth of hash+checksum
    block_hash = _sha256_b64(data)
    block_crc = _crc32_str(data)
    blocks_resp = _post(cookies, UPLOAD_HOST, '/space/api/box/upload/blocks/', {
        'upload_id': upload_id,
        'mount_point': 'docx_image',
        'blocks': [{
            'seq': 0,
            'size': image['size'],
            'hash': block_hash,
            'checksum': block_crc,
        }],
    })
    needed = blocks_resp.get('needed_upload_blocks') or []

    # 3. merge_block — binary body on the drive-stream host
    if needed:
        headers = {
            'x-seq-list': '0',
            'x-block-list-checksum': _adler32_str(data),
            'x-block-origin-size': str(block_size),
        }
        path = f'/space/api/box/stream/upload/merge_block/?upload_id={upload_id}&mount_point=docx_image'
        mb_res = http_post_binary(cookies, DOC_IMAGE_HOST, path, data, headers)
        mb_body = (mb_res.get('data') or {})
        if mb_body.get('code') != 0:
            raise RuntimeError(f'merge_block failed: {mb_body}')

    # 4. finish — returns the final file_token
    fin = _post(cookies, UPLOAD_HOST, '/space/api/box/upload/finish/', {
        'upload_id': upload_id,
        'num_blocks': 1,
        'mount_point': 'docx_image',
        'push_open_history_record': 0,
    })
    token = fin.get('file_token')
    if not token:
        raise RuntimeError(f'finish returned no file_token: {fin}')
    return token


def _post(cookies, host: str, path: str, body: dict) -> dict:
    res = http_post_with_cookies(cookies, host, path, body)
    payload = (res.get('data') or {})
    if payload.get('code') != 0:
        raise RuntimeError(f'POST {path} failed: code={payload.get("code")} msg={payload.get("message") or payload.get("msg")} body={payload}')
    return payload.get('data') or {}
