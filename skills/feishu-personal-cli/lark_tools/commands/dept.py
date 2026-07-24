"""
commands/dept.py - Department commands (mirrors lib/commands/dept.js)
"""

import json
import sys

from ..proto import encode_message
from ..gateway import send_gateway_request, decode_response
from ..formatters import format_dept_list, format_dept_children, format_dept_members


def cmd_dept_list(cookies, raw=False):
    resp = send_gateway_request(cookies, 84, encode_message({1: '0'}))
    result = decode_response(resp['buffer'])
    packet = result.get('packet')
    payload = result.get('payload')
    status = packet and packet.get('status', 0)
    success = packet is not None and (not status or status == 0 or str(status) == '0')
    if raw:
        print(json.dumps({'success': success, 'action': 'list', 'payload': payload}))
        return
    print(json.dumps({
        'success': success,
        'action': 'list',
        'departments': format_dept_list(payload),
    }))


def cmd_dept_children(cookies, dept_id: str, raw=False):
    resp = send_gateway_request(cookies, 83, encode_message({1: dept_id}))
    result = decode_response(resp['buffer'])
    packet = result.get('packet')
    payload = result.get('payload')
    status = packet and packet.get('status', 0)
    success = packet is not None and (not status or status == 0 or str(status) == '0')
    if raw:
        print(json.dumps({'success': success, 'action': 'children', 'deptId': dept_id, 'payload': payload}))
        return
    print(json.dumps({
        'success': success,
        'action': 'children',
        'deptId': dept_id,
        'departments': format_dept_children(payload),
    }))


def cmd_dept_members(cookies, dept_id: str, raw=False):
    resp = send_gateway_request(cookies, 80, encode_message({1: dept_id}))
    result = decode_response(resp['buffer'])
    packet = result.get('packet')
    payload = result.get('payload')
    status = packet and packet.get('status', 0)
    success = packet is not None and (not status or status == 0 or str(status) == '0')
    if raw:
        print(json.dumps({'success': success, 'action': 'members', 'deptId': dept_id, 'payload': payload}))
        return
    members_data = format_dept_members(payload)
    print(json.dumps({
        'success': success,
        'action': 'members',
        'deptId': dept_id,
        **members_data,
    }))
