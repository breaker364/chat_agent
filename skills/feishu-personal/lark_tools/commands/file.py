"""
commands/file.py — IM file-message download.

Counterpart to commands/img.py for files (msgType=3, entities.Message.Type.FILE).
Unlike images, files are NOT encrypted — no AES-GCM step needed. Spec lives
in references/api/rest_get_download_messages_files.md (2026-05-12 captures).

Download URL pattern:
    GET https://internal-api-lark-file.feishu.cn/download/messages/{messageId}/keys/{fileKey}

Required headers: Cookie + Origin + Referer (server enforces CORS).
Query params (chat_id, scene) are ignored by the server in practice.
"""

import json
import os
import re
import sys

from ..proto import encode_message, decode_packet, extract_raw_field
from ..gateway import send_gateway_request
from ..http_utils import https_get


FILE_DOWNLOAD_HOST = 'internal-api-lark-file.feishu.cn'


def build_download_url(message_id: str, file_key: str) -> str:
    """Build the file-download URL. messageId / fileKey are inserted verbatim
    (server-side characters are safe; no URL-encoding required)."""
    return (
        f'https://{FILE_DOWNLOAD_HOST}'
        f'/download/messages/{message_id}/keys/{file_key}'
    )


def parse_filename_from_content_disposition(header: str) -> str:
    """Extract the filename from a Content-Disposition header.

    Feishu sends UTF-8 bytes packed into a latin-1-encoded `filename="..."`
    value (NOT the RFC 5987 `filename*=UTF-8''<urlencoded>` form), so we
    must round-trip through latin-1 to recover the real UTF-8 string.

    Returns '' if no filename can be parsed.
    """
    if not header:
        return ''
    # filename*= (RFC 5987) wins over plain filename= when both present.
    m_ext = re.search(r"filename\*=([^']*)'[^']*'([^;]+)", header)
    if m_ext:
        from urllib.parse import unquote
        encoding = (m_ext.group(1) or 'utf-8').strip()
        try:
            return unquote(m_ext.group(2).strip(), encoding=encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    m = re.search(r'filename="([^"]+)"', header)
    if not m:
        # Some servers omit the quotes — try unquoted form.
        m = re.search(r'filename=([^;]+)', header)
        if not m:
            return ''
    raw = m.group(1).strip()
    try:
        # latin-1 ⇒ bytes ⇒ utf-8. Only round-trip if the literal contains
        # high bytes; pure-ASCII filenames pass through unchanged.
        return raw.encode('latin-1').decode('utf-8')
    except UnicodeError:
        # Already valid utf-8 (or genuinely latin-1) — return as-is.
        return raw


def fetch_file_meta_from_msg(cookies, message_id: str) -> dict:
    """Pull a single message via cmd 8 and extract file metadata.

    Returns {fileKey, filename, size, mime} for the file message, or None
    if the message isn't a file message / doesn't exist / fails to decode.
    """
    resp = send_gateway_request(cookies, 8, encode_message({1: [message_id]}))
    packet = decode_packet(resp['buffer'])
    raw_payload = packet.get('payload')
    if not raw_payload:
        return None
    # payload.f1 is repeated; take the first.
    wrapper = extract_raw_field(raw_payload, 1, 2)
    if not wrapper:
        return None
    # Message body may be wrapped at f2 or directly under f1.
    msg_buf = extract_raw_field(wrapper, 2, 2) or wrapper
    return _extract_file_meta(msg_buf)


def _extract_file_meta(msg_buf) -> dict:
    """Pull {fileKey, filename, size, mime} out of a raw Message proto, if it
    is a file message (msgType==3). Returns None otherwise.

    File message schema (Content lives directly at f5, no inner wrapper —
    different from image where Content sits at f5.f2):
        Message.f2 = msgType (must be 3)
        Message.f5 = Content { f1: fileKey, f2: filename, f3: size, f4: mime }
    """
    type_v = extract_raw_field(msg_buf, 2, 0)
    type_s = extract_raw_field(msg_buf, 2, 2)
    type_str = type_v or (type_s.decode('utf-8', errors='replace') if type_s else '')
    if str(type_str) != '3':
        return None

    content_buf = extract_raw_field(msg_buf, 5, 2)
    if not content_buf:
        return None

    file_key_b = extract_raw_field(content_buf, 1, 2)
    if not file_key_b:
        return None
    filename_b = extract_raw_field(content_buf, 2, 2)
    size_v = extract_raw_field(content_buf, 3, 0)
    mime_b = extract_raw_field(content_buf, 4, 2)

    return {
        'fileKey': file_key_b.decode('utf-8', errors='replace'),
        'filename': filename_b.decode('utf-8', errors='replace') if filename_b else '',
        'size': int(size_v) if size_v is not None else None,
        'mime': mime_b.decode('utf-8', errors='replace') if mime_b else '',
    }


def _safe_filename(name: str, fallback: str) -> str:
    """Strip path separators + control chars so a server-provided filename
    can't escape the target directory or break tooling."""
    if not name:
        return fallback
    # Drop any directory components — only the basename is safe.
    name = os.path.basename(name)
    # Strip control chars.
    name = re.sub(r'[\x00-\x1f]', '', name).strip()
    return name or fallback


def cmd_download_file(cookies, file_key: str, message_id: str,
                       output_path: str = None):
    """Download a chat file by (messageId, fileKey).

    Both arguments are required — the download URL embeds messageId as a
    path segment (`/download/messages/{messageId}/keys/{fileKey}`), so
    fileKey alone is not enough. Fetch the messageId from `lark chat
    messages <chatId>` (default-mode output already surfaces it for
    file messages).

    The server returns the raw file bytes (no decryption). Filename is
    taken from the response's Content-Disposition unless `output_path`
    is provided.
    """
    if not message_id:
        print(json.dumps({'error': 'NEED_MSG',
                           'message': 'Provide --msg <messageId> '
                                      '(run `lark chat messages <chatId>` to find it)'}))
        return

    cookie_str = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
    url = build_download_url(message_id, file_key)
    resp = https_get(url, {
        'Cookie': cookie_str,
        'Origin': 'https://nio.feishu.cn',
        'Referer': 'https://nio.feishu.cn/',
    })

    # 200 = full body; 206 only happens if caller sends Range header (we don't).
    if resp['statusCode'] not in (200, 206):
        print(json.dumps({'success': False, 'error': f"HTTP {resp['statusCode']}",
                           'fileKey': file_key, 'messageId': message_id,
                           'url': url}))
        return

    buf = resp['buffer']
    headers = resp.get('headers') or {}
    content_disposition = headers.get('Content-Disposition') or headers.get('content-disposition') or ''
    content_type = headers.get('Content-Type') or headers.get('content-type') or ''

    server_filename = parse_filename_from_content_disposition(content_disposition)

    from ..paths import resolve_output_path
    fname = _safe_filename(server_filename, fallback=file_key)
    file_path = os.path.abspath(resolve_output_path(output_path, fname))

    with open(file_path, 'wb') as f:
        f.write(buf)

    print(json.dumps({
        'success': True,
        'fileKey': file_key,
        'messageId': message_id,
        'path': file_path,
        'size': len(buf),
        'filename': server_filename or None,
        'contentType': content_type or None,
    }))
