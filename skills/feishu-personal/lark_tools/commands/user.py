"""
commands/user.py - User profile commands (mirrors lib/commands/user.js)
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..proto import encode_message
from ..gateway import send_gateway_request, decode_response

CONCURRENCY = 10


def _extract_profile(payload):
    if not isinstance(payload, dict) or not payload.get('f2'):
        return None
    p = payload['f2']
    if not isinstance(p, dict):
        return None
    return {
        'userId': p.get('f1', ''),
        'name': p.get('f2', '') if isinstance(p.get('f2'), str) else '',
        'englishName': p.get('f14', ''),
        'email': p.get('f25', ''),
    }


def _fetch_one(cookies, uid):
    resp = send_gateway_request(cookies, 5031, encode_message({1: 1, 2: uid}))
    result = decode_response(resp['buffer'])
    return _extract_profile(result.get('payload'))


def cmd_user(cookies, user_ids: list, raw=False):
    if len(user_ids) == 1:
        resp = send_gateway_request(cookies, 5031, encode_message({1: 1, 2: user_ids[0]}))
        result = decode_response(resp['buffer'])
        packet = result.get('packet')
        payload = result.get('payload')
        status = packet and packet.get('status', 0)
        success = packet is not None and (not status or status == 0 or str(status) == '0')
        if raw:
            print(json.dumps({'success': success, 'userId': user_ids[0], 'payload': payload}))
            return
        print(json.dumps({'success': success, 'user': _extract_profile(payload)}))
    else:
        # Parallel fetches
        results = [None] * len(user_ids)
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
            futures = {executor.submit(_fetch_one, cookies, uid): i for i, uid in enumerate(user_ids)}
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    results[idx] = future.result()
                except Exception:
                    results[idx] = None
        if raw:
            print(json.dumps({'success': True, 'userIds': user_ids, 'results': results}))
            return
        users = [r for r in results if r]
        print(json.dumps({'success': True, 'users': users}))


def batch_resolve_user_names(cookies, user_ids: list) -> dict:
    """Fetch names for multiple userIds in parallel. Returns {userId: name}."""
    unique = list(dict.fromkeys(uid for uid in user_ids if uid))
    if not unique:
        return {}
    name_map = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {executor.submit(_fetch_one_name, cookies, uid): uid for uid in unique}
        for future in as_completed(futures):
            uid = futures[future]
            try:
                name = future.result()
                if name:
                    name_map[uid] = name
            except Exception:
                pass
    return name_map


def _fetch_one_name(cookies, uid: str):
    try:
        resp = send_gateway_request(cookies, 5031, encode_message({1: 1, 2: uid}))
        result = decode_response(resp['buffer'])
        payload = result.get('payload')
        name = payload and isinstance(payload, dict) and payload.get('f2', {})
        if isinstance(name, dict):
            name = name.get('f2')
        return name if isinstance(name, str) else ''
    except Exception:
        return ''
