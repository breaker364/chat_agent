"""
im_upload.py - IM image/file upload (3-step protocol).

All 3 endpoints share host `internal-api-lark-file.feishu.cn`:
  1. POST /upload/create    (protobuf body: filename/size/blake3 hash, media type)
  2. POST /upload/part?upload_id=X&part_id=N&offset=M  (raw octet-stream body)
  3. POST /upload/complete  (protobuf body: upload_id/crc32/mime/filename)

Returns `upload_id` that is then transformed into a cmd 5 `f2` key:
  - image: "R1" + upload_id.replace("_0010", "_0210")   e.g. R1img_v3_0210s_<uuid>g
  - file:  "22" + upload_id (no middle change)          e.g. 22file_v3_0010t_<uuid>g
"""

import mimetypes
import os
import sys
import requests

from .proto import encode_message, generic_decode

IM_UPLOAD_HOST = 'internal-api-lark-file.feishu.cn'

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'

# Single source of truth for the shared headers (x-lgw-*, x-appId, ...).
_SHARED_HEADERS = {
    'x-lgw-terminal-type': '2',
    'x-lgw-os-type': '3',
    'x-lsc-bizid': '1',
    'x-lsc-version': '1',
    'x-source': 'web',
    'x-web-version': '7.65.0',
    'x-appId': '161471',
    'locale': 'zh-CN',
    'User-Agent': UA,
    'Origin': 'https://www.feishu.cn',
    'Referer': 'https://www.feishu.cn/',
}


# Media type enum for /upload/create f3 and /upload/complete f2.
TYPE_FILE = 1
TYPE_IMAGE = 2


def _cookie_header(cookies):
    return '; '.join(f"{c['name']}={c['value']}" for c in cookies)


def _blake3_hash(data: bytes) -> bytes:
    from blake3 import blake3
    return blake3(data).digest()  # 32 raw bytes


def _post_pb(cookies, path: str, payload: bytes) -> bytes:
    headers = dict(_SHARED_HEADERS)
    headers['Content-Type'] = 'application/x-protobuf'
    headers['Cookie'] = _cookie_header(cookies)
    resp = requests.post(f'https://{IM_UPLOAD_HOST}{path}', data=payload, headers=headers, timeout=60, verify=True)
    if resp.status_code != 200:
        raise RuntimeError(f'upload {path} HTTP {resp.status_code}: {resp.text[:200]}')
    return resp.content


def upload_create(cookies, file_bytes: bytes, filename: str, media_type: int) -> str:
    """Step 1: declare upload. Returns upload_id."""
    file_hash = _blake3_hash(file_bytes)
    # f9 value = literal "blake3:" + raw 32-byte hash (bytes concatenated, NOT hex)
    hash_field = b'blake3:' + file_hash

    req = {
        3: media_type,
        5: filename,
        6: len(file_bytes),
        9: hash_field,
    }
    resp_bytes = _post_pb(cookies, '/upload/create', encode_message(req))
    decoded = generic_decode(resp_bytes) or {}
    upload_id = decoded.get('f1')
    if not upload_id or not isinstance(upload_id, str):
        raise RuntimeError(f'upload/create: no upload_id in response: {decoded}')
    return upload_id


def upload_part(cookies, upload_id: str, body_bytes: bytes, part_id: int = 1, offset: int = None) -> int:
    """Step 2: send raw bytes. Returns server-computed CRC32 (for echo in /complete)."""
    if offset is None:
        offset = len(body_bytes)  # single-part case: offset = total size
    headers = dict(_SHARED_HEADERS)
    headers['Content-Type'] = 'application/octet-stream'
    headers['Cookie'] = _cookie_header(cookies)
    url = f'https://{IM_UPLOAD_HOST}/upload/part?upload_id={upload_id}&part_id={part_id}&offset={offset}'
    resp = requests.post(url, data=body_bytes, headers=headers, timeout=120, verify=True)
    if resp.status_code != 200:
        raise RuntimeError(f'upload/part HTTP {resp.status_code}: {resp.text[:200]}')
    decoded = generic_decode(resp.content) or {}
    crc_str = decoded.get('f2')
    if crc_str is None:
        raise RuntimeError(f'upload/part: no crc32 in response: {decoded}')
    try:
        return int(crc_str)
    except (ValueError, TypeError):
        raise RuntimeError(f'upload/part: bad crc32 value {crc_str!r}')


def upload_complete(cookies, upload_id: str, media_type: int,
                     part_crc32s: dict, filename: str, mime: str,
                     image_keys: dict = None) -> tuple:
    """Step 3: finalize. Returns (ok, code, crypto_token).
      - ok: True iff code == 0 (SUCCESS)
      - code: FileUploadCode enum (0=SUCCESS, 1=MISSING_PART, 2=CRC32_FAIL, 3=FAIL)
      - crypto_token: server-returned string from response.f2 — THIS is what
        goes into cmd 5 Content.cryptoToken for images, NOT a derived upload_id.

    part_crc32s: dict {partId -> crc32}. Encoded as protobuf map wire (repeated
    MapEntry with f1=key, f2=value), NOT as {f1: num, f2: crc}.
    """
    # map<uint32, uint32> at field 3 wire = field 3 REPEATED, each value a MapEntry{f1:key, f2:value}.
    # Do NOT wrap in an extra container message.
    map_entries = [{1: pid, 2: crc} for pid, crc in sorted(part_crc32s.items())]
    req = {
        1: upload_id,
        2: media_type,
        3: map_entries,         # repeated MapEntry at field 3
        4: filename,
        5: mime,
    }
    if image_keys:
        # ImageKeys at f7: {originKey=1, middleKey=2, thumbnailKey=3, webpMiddleKey=4,
        # webpThumbnailKey=5, middleMp4Key=6, coverKey=7, intactKey=8}
        ik_fields = {}
        for k, fid in (('origin', 1), ('middle', 2), ('thumbnail', 3),
                       ('webpMiddle', 4), ('webpThumbnail', 5), ('middleMp4', 6),
                       ('cover', 7), ('intact', 8)):
            if image_keys.get(k):
                ik_fields[fid] = image_keys[k]
        if ik_fields:
            req[7] = ik_fields
    resp_bytes = _post_pb(cookies, '/upload/complete', encode_message(req))
    decoded = generic_decode(resp_bytes) or {}
    code_raw = decoded.get('f1')
    try:
        code = int(code_raw) if code_raw is not None else -1
    except (TypeError, ValueError):
        code = -1
    crypto_token = decoded.get('f2') if isinstance(decoded.get('f2'), str) else None
    return code == 0, code, crypto_token


def guess_media_type(path: str) -> tuple:
    """Returns (TYPE_IMAGE|TYPE_FILE, mime)."""
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        mime = 'application/octet-stream'
    if mime.startswith('image/'):
        return TYPE_IMAGE, mime
    return TYPE_FILE, mime


def upload_local_file(cookies, path: str) -> dict:
    """High-level: read local file, do all 3 steps, return upload info + cmd 5 key.

    Returns: {
        'uploadId': str,
        'messageKey': str,   # ready to go into cmd 5 f2
        'isImage': bool,
        'filename': str,
        'size': int,
        'mime': str,
    }
    """
    with open(path, 'rb') as f:
        data = f.read()
    filename = os.path.basename(path)
    media_type, mime = guess_media_type(path)
    is_image = (media_type == TYPE_IMAGE)

    print(f'[im_upload] {filename} ({len(data)} bytes, {mime}, {"image" if is_image else "file"})', file=sys.stderr)

    upload_id = upload_create(cookies, data, filename, media_type)
    print(f'[im_upload] create -> upload_id={upload_id}', file=sys.stderr)

    crc32 = upload_part(cookies, upload_id, data, part_id=1, offset=len(data))
    print(f'[im_upload] part -> crc32={crc32}', file=sys.stderr)

    # No client-generated imageKeys — server returns its own cryptoToken at
    # /upload/complete response.f2, which is what cmd 5 actually needs.
    image_keys = None
    ok, code, crypto_token = upload_complete(cookies, upload_id, media_type, {1: crc32},
                                               filename, mime, image_keys=image_keys)
    if not ok:
        code_names = {1: 'MISSING_PART', 2: 'CRC32_FAIL', 3: 'FAIL'}
        raise RuntimeError(f'upload/complete returned non-SUCCESS code={code} ({code_names.get(code, "?")})')
    print(f'[im_upload] complete -> ok, cryptoToken={crypto_token}', file=sys.stderr)
    return {
        'uploadId': upload_id,
        'cryptoToken': crypto_token,  # server-returned; use this for cmd 5 image
        'isImage': is_image,
        'filename': filename,
        'size': len(data),
        'mime': mime,
    }
