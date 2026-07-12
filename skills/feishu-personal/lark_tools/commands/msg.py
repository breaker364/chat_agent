"""
commands/msg.py - Single message detail (mirrors lib/commands/msg.js)
"""

import json

from ..proto import encode_message
from ..gateway import send_gateway_request, decode_response
from ..formatters import format_message_detail


def cmd_msg(cookies, message_id: str, raw=False):
    resp = send_gateway_request(cookies, 8, encode_message({1: message_id}))
    result = decode_response(resp['buffer'])
    packet = result.get('packet')
    payload = result.get('payload')
    status = packet and packet.get('status', 0)
    success = packet is not None and (not status or status == 0 or str(status) == '0')
    if raw:
        print(json.dumps({'success': success, 'messageId': message_id, 'payload': payload}))
        return
    message = format_message_detail(payload)
    print(json.dumps({'success': success, 'message': message}))
