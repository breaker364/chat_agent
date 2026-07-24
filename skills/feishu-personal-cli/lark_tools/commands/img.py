"""
commands/img.py - Image download & decrypt (mirrors lib/commands/img.js)
"""

import json
import os
import sys

from ..proto import encode_message, extract_raw_field, decode_packet
from ..gateway import send_gateway_request
from ..crypto import extract_image_crypto, extract_embedded_image_crypto, decrypt_image_buffer
from ..http_utils import https_get, http_get_binary


def cmd_download_image(cookies, image_key: str, output_path=None, crypto_info=None):
    cookie_str = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
    url = f'https://internal-api-lark-file.feishu.cn/static-resource/v1/{image_key}~?image_size=noop&format=image'
    resp = https_get(url, {
        'Cookie': cookie_str,
        'Origin': 'https://nio.feishu.cn',
        'Referer': 'https://nio.feishu.cn/',
    })

    if resp['statusCode'] != 200:
        print(json.dumps({'success': False, 'error': f"HTTP {resp['statusCode']}", 'imageKey': image_key}))
        return

    image_buf = resp['buffer']
    decrypted = False

    if crypto_info and crypto_info.get('secretKey') and crypto_info.get('secretNonce'):
        try:
            image_buf = decrypt_image_buffer(
                image_buf,
                crypto_info['secretKey'],
                crypto_info['secretNonce'],
                crypto_info.get('cipherType', 1),
            )
            decrypted = True
        except Exception as e:
            print(f'[img] Decryption failed: {e}', file=sys.stderr)

    # Detect image type from magic bytes
    ext = '.bin'
    if len(image_buf) >= 4:
        magic = image_buf[:4].hex().upper()
        if magic.startswith('FFD8FF'):
            ext = '.jpg'
        elif magic == '89504E47':
            ext = '.png'
        elif magic == '47494638':
            ext = '.gif'
        elif magic == '52494646':
            ext = '.webp'

    from ..paths import resolve_output_path
    file_path = os.path.abspath(resolve_output_path(output_path, f'{image_key}{ext}'))

    with open(file_path, 'wb') as f:
        f.write(image_buf)

    content_type = resp['headers'].get('content-type', '')
    print(json.dumps({
        'success': True,
        'imageKey': image_key,
        'path': file_path,
        'size': len(image_buf),
        'decrypted': decrypted,
        'contentType': content_type,
    }))


def download_sheet_image(cookies, image_token: str, spreadsheet_token: str, output_path=None):
    """Download an image embedded in a spreadsheet cell.

    Unlike chat images (which use `static-resource/v1/{key}~?...`),
    spreadsheet images are served through the "cover" download endpoint with
    a `mount_node_token` context.  This endpoint returns the full-resolution
    image (the static-resource endpoint returns a 1000×595 thumbnail).

    Parameters
    ----------
    cookies : list
        Feishu session cookies (from auth.load_cookies()).
    image_token : str
        The image token extracted from f12.f3 protobuf entries
        (see fetch_sheet_images in commands/sheet.py).
    spreadsheet_token : str
        The spreadsheet token (from the sheet URL).
    output_path : str or None
        Output file path. If None or empty, defaults to
        `{image_token[:12]}.png` in the current directory.

    Returns
    -------
    dict with keys: success, path, size, contentType, token, dimensions

    Raises
    ------
    RuntimeError on HTTP failure.

    Notes
    -----
    The cover URL was discovered by reverse-engineering the network requests
    made by the Feishu browser client when viewing an image in a spreadsheet
    cell at full resolution. Key parameters:

    * ``height`` / ``width`` — request dimensions. Use 4096 for "as large as
      available" (server caps at the original resolution).
    * ``mount_node_token`` — the spreadsheet token, required for authorization.
    * ``mount_point=sheet_image`` — tells the server this is a sheet image.
    * ``policy=equal`` — maintain aspect ratio.

    If the static-resource endpoint (used by cmd_download_image) is tried on a
    sheet image token, it returns HTTP 400 (invalid token format). Conversely,
    chat message image tokens also work with the cover endpoint if you provide
    the right mount point context, but prefer the static-resource path for
    those as it does not require a mount_node_token.
    """
    from ..paths import resolve_output_path

    url = (
        f'https://internal-api-drive-stream.feishu.cn/space/api/box/stream/'
        f'download/v2/cover/{image_token}/?'
        f'height=4096&mount_node_token={spreadsheet_token}'
        f'&mount_point=sheet_image&policy=equal&width=4096'
    )

    resp = http_get_binary(cookies, url)

    if resp['status'] != 200:
        msg = f"Sheet image download failed: HTTP {resp['status']} for token {image_token[:20]}..."
        print(f'[img] {msg}', file=sys.stderr)
        raise RuntimeError(msg)

    image_buf = resp['buffer']

    # Detect image type from magic bytes
    ext = '.png'
    if len(image_buf) >= 4:
        magic = image_buf[:4].hex().upper()
        if magic.startswith('FFD8FF'):
            ext = '.jpg'
        elif magic == '89504E47':
            ext = '.png'
        elif magic == '47494638':
            ext = '.gif'
        elif magic == '52494646':
            ext = '.webp'

    default_name = f'{image_token[:12]}{ext}'
    file_path = os.path.abspath(resolve_output_path(output_path, default_name))

    with open(file_path, 'wb') as f:
        f.write(image_buf)

    # Extract dimensions from PNG IHDR
    w, h = 0, 0
    if ext == '.png' and len(image_buf) > 24:
        w = int.from_bytes(image_buf[16:20], 'big')
        h = int.from_bytes(image_buf[20:24], 'big')

    content_type = resp.get('contentType', '')
    result = {
        'success': True,
        'token': image_token,
        'path': file_path,
        'size': len(image_buf),
        'contentType': content_type,
        'dimensions': f'{w}x{h}',
    }
    print(json.dumps(result))
    return result


def fetch_image_crypto(cookies, message_id: str, image_key: str):
    """Fetch a message's raw payload and extract crypto info for a given imageKey."""
    resp = send_gateway_request(cookies, 8, encode_message({1: message_id}))
    packet = decode_packet(resp['buffer'])
    raw_payload = packet.get('payload')
    if not raw_payload:
        return None

    # Navigate: payload.f1 (wrapper) -> f2 (message) or directly f1
    wrapper = extract_raw_field(raw_payload, 1, 2)
    if not wrapper:
        return None

    msg_buf = extract_raw_field(wrapper, 2, 2) or wrapper

    # Check message type
    msg_type_v = extract_raw_field(msg_buf, 2, 0)   # varint
    msg_type_s = extract_raw_field(msg_buf, 2, 2)   # string
    type_str = msg_type_v or (msg_type_s.decode('utf-8', errors='replace') if msg_type_s else '')

    if str(type_str) == '5':
        return extract_image_crypto(msg_buf)
    else:
        return extract_embedded_image_crypto(msg_buf, image_key)
