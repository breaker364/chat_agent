"""
commands/search.py - Search commands (mirrors lib/commands/search.js)
"""

import json
import math
import random
import string
import sys

from ..proto import encode_message
from ..gateway import send_gateway_request, decode_response
from ..formatters import (
    format_search_contacts, format_search_messages, format_search_docs,
    format_search_groups, format_search_vc, parse_total,
)

SEARCH_CONFIGS = {
    'contacts': {
        'tagName': 'SEARCH_CHATTERS_IN_ADVANCE_SCENE',
        'entityItems': [{1: 1}, {1: 31}, {1: 24}],
    },
    'messages': {
        'tagName': 'SEARCH_MESSAGES',
        'entityItems': [{1: 5}, {1: 24}],
    },
    'docs': {
        'tagName': 'SEARCH_DOC',
        'entityItems': [{1: 7}, {1: 8}, {1: 24}],
    },
    'groups': {
        'tagName': 'SEARCH_CHATS_IN_ADVANCE_SCENE',
        'entityItems': [{1: 3}, {1: 24}],
    },
    'apps': {
        'tagName': 'SEARCH_OPEN_APP_SCENE',
        'entityItems': [{1: 9}, {1: 2}, {1: 24}],
    },
    'vc': {
        'tagName': 'SEARCH_OPEN_SEARCH_SCENE',
        'entityItems': None,
        'vcCommandId': '7359496317812375580',
    },
    'smart': {
        'tagName': 'SMART_SEARCH',
        'entityItems': [
            {1: 1}, {1: 3}, {1: 4}, {1: 27},
            {1: 9}, {1: 2}, {1: 10}, {1: 22},
            {1: 31}, {1: 24},
        ],
    },
}


def _rand_session():
    return 'cli_' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))


def cmd_search(cookies, query: str, type_: str, raw=False, opts=None):
    opts = opts or {}
    config = SEARCH_CONFIGS[type_]
    session_id = _rand_session()
    limit = opts.get('limit', 300)
    max_pages = math.ceil(limit / 20)

    import time as _time
    entity_items = config.get('entityItems')

    # VC: build entity items with dynamic time range
    if type_ == 'vc':
        now = int(_time.time())
        thirty_days_ago = now - 30 * 86400
        sort_config = encode_message({
            1: 'meeting_start_time',
            3: encode_message({1: thirty_days_ago, 2: now}),
        })
        entity_items = [
            {1: 16, 2: {7: {1: config['vcCommandId'], 3: sort_config, 4: 0}}},
            {1: 24},
        ]

    # Messages with time range / noBot filter
    if (opts.get('timeRange') or opts.get('noBot')) and type_ == 'messages':
        new_items = []
        for item in entity_items:
            if isinstance(item, dict) and item.get(1) == 5:
                msg_filter = {}
                if opts.get('timeRange'):
                    tr = opts['timeRange']
                    time_range_bytes = encode_message({1: tr['from'], 2: tr['to']})
                    msg_filter[1] = time_range_bytes
                if opts.get('noBot'):
                    msg_filter[8] = 1
                new_items.append({1: 5, 2: {3: msg_filter}})
            else:
                new_items.append(item)
        entity_items = new_items

    formatters = {
        'contacts': format_search_contacts,
        'messages': format_search_messages,
        'docs': format_search_docs,
        'groups': format_search_groups,
        'vc': format_search_vc,
    }
    formatter = formatters.get(type_)

    all_results = []
    pagination_token = None
    total = 0

    for seq_id in range(1, max_pages + 1):
        search_hint = {1: query, 2: seq_id - 1}
        if opts.get('timeRange'):
            search_hint[3] = {11: {1: '1'}}
        if opts.get('noBot'):
            if 3 not in search_hint:
                search_hint[3] = {}
            search_hint[3][15] = {1: '2'}

        search_config = {
            1: config['tagName'],
            2: entity_items,
            6: search_hint,
        }
        if type_ == 'vc':
            search_config[3] = {1: 1, 8: 1, 9: 1}

        search_request = {
            1: session_id,
            2: seq_id,
            3: query,
            5: search_config,
            6: 'zh_CN',
            15: 2,
            16: 'Asia/Shanghai',
            18: '1',
        }
        if type_ == 'vc':
            search_request[8] = {6: 1, 7: 1, 10: 0, 12: 1, 13: 1}
            search_request[9] = {2: 200}
            search_request[10] = {2: 202}
        if pagination_token:
            search_request[4] = pagination_token

        payload_bytes = encode_message({1: search_request})
        resp = send_gateway_request(cookies, 11021, payload_bytes)
        result = decode_response(resp['buffer'])
        packet = result.get('packet')
        resp_payload = result.get('payload')
        status = packet and packet.get('status', 0)
        success = packet is not None and (not status or status == 0 or str(status) == '0')

        if not success:
            if seq_id == 1:
                print(json.dumps({'error': 'SEARCH_FAILED', 'status': status, 'type': type_, 'query': query}))
                sys.exit(1)
            break

        if raw:
            print(json.dumps({'success': True, 'type': type_, 'query': query, 'payload': resp_payload}))
            return

        if seq_id == 1:
            total = parse_total(resp_payload.get('f1') if isinstance(resp_payload, dict) else None) or 0

        page_results = formatter(resp_payload) if formatter else ([resp_payload] if resp_payload else [])
        if not page_results:
            break

        prev_len = len(all_results)
        seen = {
            r.get('meetingId') or r.get('messageId') or r.get('userId') or r.get('chatId') or r.get('token') or json.dumps(r, sort_keys=True)
            for r in all_results
        }
        for r in page_results:
            key = r.get('meetingId') or r.get('messageId') or r.get('userId') or r.get('chatId') or r.get('token') or json.dumps(r, sort_keys=True)
            if key not in seen:
                all_results.append(r)
                seen.add(key)
        new_count = len(all_results) - prev_len

        print(f'[search] Page {seq_id}: {len(page_results)} results, {new_count} new, total {len(all_results)} unique', file=sys.stderr)

        has_more = False
        try:
            info_str = isinstance(resp_payload, dict) and resp_payload.get('f1', {})
            if isinstance(info_str, dict):
                info_str = info_str.get('f5')
            if info_str and isinstance(info_str, str):
                info = json.loads(info_str)
                has_more = bool(info.get('HasMore'))
                pagination_token = info_str
        except Exception:
            pass

        if not has_more or new_count == 0 or len(all_results) >= limit:
            break

    if len(all_results) > limit:
        all_results = all_results[:limit]

    print(json.dumps({
        'success': True,
        'type': type_,
        'query': query,
        'total': total,
        'returned': len(all_results),
        'results': all_results,
    }))
