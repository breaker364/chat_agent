"""
gateway.py - Feishu im/gateway HTTP protobuf request handler (mirrors lib/gateway.js)
"""

import uuid
import requests

from .config import GATEWAY_HOST
from .proto import encode_packet, decode_packet, generic_decode

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'


def send_gateway_request(cookies: list, cmd: int, payload: bytes) -> dict:
    """
    Send a protobuf-encoded request to im/gateway.
    Returns {statusCode, buffer, cid}.
    """
    cid = str(uuid.uuid4())
    body = encode_packet(payload_type=1, cmd=cmd, payload=payload or b'', cid=cid)

    cookie_str = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
    request_id = str(uuid.uuid4())

    headers = {
        'Content-Type': 'application/x-protobuf',
        'X-Command': str(cmd),
        'X-Request-Id': request_id,
        'X-Source': 'web',
        'x-command-version': '7.65.0',
        'x-web-version': '7.65.0',
        'x-lgw-terminal-type': '2',
        'x-lgw-os-type': '3',
        'locale': 'zh_CN',
        'Cookie': cookie_str,
        'User-Agent': UA,
        'Origin': 'https://nio.feishu.cn',
        'Referer': 'https://nio.feishu.cn/',
    }

    resp = requests.post(
        f'https://{GATEWAY_HOST}/im/gateway/',
        data=body,
        headers=headers,
        timeout=30,
        verify=True,
    )
    return {
        'statusCode': resp.status_code,
        'buffer': resp.content,
        'cid': cid,
    }


def decode_response(buffer: bytes) -> dict:
    """
    Decode a gateway response buffer.
    Returns {packet, payload} where packet has raw fields and payload is generic-decoded.
    """
    try:
        packet = decode_packet(buffer)
        payload = None
        if packet.get('payload'):
            payload = generic_decode(packet['payload'])
        return {'packet': packet, 'payload': payload}
    except Exception:
        return {'packet': None, 'payload': generic_decode(buffer)}
