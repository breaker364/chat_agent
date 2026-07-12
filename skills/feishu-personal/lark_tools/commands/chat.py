"""
commands/chat.py - Chat history commands (mirrors lib/commands/chat.js)
"""

import json
import math
import random
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from ..proto import encode_message, decode_varint
from ..gateway import send_gateway_request, decode_response
from ..formatters import (
    format_search_messages, format_search_groups,
    format_chat_messages, strip_highlight, to_array,
)
from .search import SEARCH_CONFIGS
from .user import batch_resolve_user_names
from ..auth import get_current_user_id


def _rand_session(prefix='cli'):
    return prefix + '_' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))


def _project_message_default(m: dict) -> dict:
    """Default (non-verbose) per-message projection.

    Keeps the output lean for pure text but auto-includes the keys needed
    to *act* on downloadable resources — so users can copy-paste straight
    from a chat listing into `lark img --msg ...` or `lark file --msg ...`
    without re-running with `--verbose`.

    Field set:
      always:                    sender, time, content
      image messages (type=5):   + imageKey, messageId
      file messages (type=3):    + fileKey, filename, messageId
    """
    out = {
        'sender': m.get('sender') or m.get('senderId') or '',
        'time': m.get('time'),
        'content': m.get('content'),
    }
    has_resource = False
    if m.get('imageKey'):
        out['imageKey'] = m['imageKey']
        has_resource = True
    if m.get('fileKey'):
        out['fileKey'] = m['fileKey']
        if m.get('filename'):
            out['filename'] = m['filename']
        has_resource = True
    if has_resource and m.get('messageId'):
        out['messageId'] = m['messageId']
    return out


# ---------------------------------------------------------------------------
# search_messages_page
# ---------------------------------------------------------------------------

def search_messages_page(cookies, query, seq_id, time_range, session_id,
                          pagination_token, no_bot=False, chat_type=None,
                          sender_user_id=None, at_user_id=None):
    entity_items = [{1: 5}, {1: 24}]
    if time_range or no_bot or chat_type or sender_user_id or at_user_id:
        msg_filter = {}
        if time_range:
            msg_filter[1] = encode_message({1: time_range['from'], 2: time_range['to']})
        if sender_user_id:
            msg_filter[2] = sender_user_id
        if at_user_id:
            msg_filter[6] = at_user_id
        if no_bot:
            msg_filter[8] = 1
        if chat_type == 'p2p':
            msg_filter[10] = 2
        elif chat_type == 'group':
            msg_filter[10] = 1
        entity_items = [{1: 5, 2: {3: msg_filter}}, {1: 24}]

    search_hint = {1: query, 2: seq_id - 1}
    if time_range:
        search_hint[3] = {11: {1: '1'}}
    if no_bot:
        if 3 not in search_hint:
            search_hint[3] = {}
        search_hint[3][15] = {1: '2'}
    if sender_user_id:
        if 3 not in search_hint:
            search_hint[3] = {}
        search_hint[3][12] = {1: sender_user_id}
    if at_user_id:
        if 3 not in search_hint:
            search_hint[3] = {}
        search_hint[3][15] = {1: '1'}
    if chat_type == 'p2p':
        if 3 not in search_hint:
            search_hint[3] = {}
        search_hint[3][16] = {1: '2'}
    elif chat_type == 'group':
        if 3 not in search_hint:
            search_hint[3] = {}
        search_hint[3][16] = {1: '1'}

    search_config = {1: 'SEARCH_MESSAGES', 2: entity_items, 6: search_hint}
    search_request = {
        1: session_id, 2: seq_id, 3: query,
        5: search_config, 6: 'zh_CN', 15: 2,
        16: 'Asia/Shanghai', 18: '1',
    }
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
        return {'results': [], 'hasMore': False, 'total': 0, 'nextToken': None}

    results = format_search_messages(resp_payload)
    has_more, total, next_token = False, 0, None
    try:
        f1 = isinstance(resp_payload, dict) and resp_payload.get('f1', {})
        info_str = isinstance(f1, dict) and f1.get('f5')
        if info_str and isinstance(info_str, str):
            info = json.loads(info_str)
            has_more = bool(info.get('HasMore'))
            total = info.get('total', 0)
            next_token = info_str
    except Exception:
        pass
    return {'results': results, 'hasMore': has_more, 'total': total, 'nextToken': next_token}


# ---------------------------------------------------------------------------
# get_today_time_range
# ---------------------------------------------------------------------------

def get_today_time_range():
    import time
    from datetime import datetime, timezone, timedelta
    shanghai_tz = timezone(timedelta(hours=8))
    now_sh = datetime.now(shanghai_tz)
    date_str = now_sh.strftime('%Y-%m-%d')
    from_ts = int(datetime(now_sh.year, now_sh.month, now_sh.day, 0, 0, 0,
                            tzinfo=shanghai_tz).timestamp())
    to_ts = int(datetime(now_sh.year, now_sh.month, now_sh.day, 23, 59, 59,
                          tzinfo=shanghai_tz).timestamp())
    return {'from': from_ts, 'to': to_ts, 'dateStr': date_str}


# ---------------------------------------------------------------------------
# fetch_recent_chat_ids
# ---------------------------------------------------------------------------

def fetch_recent_chat_ids(cookies, count=20):
    req_payload = {1: 1, 2: 1, 3: 0, 4: count, 5: 0, 7: 1, 10: 1, 11: 0}
    payload_bytes = encode_message(req_payload)
    resp = send_gateway_request(cookies, 1000, payload_bytes)
    result = decode_response(resp['buffer'])
    resp_payload = result.get('payload')
    feed_items = to_array(isinstance(resp_payload, dict) and resp_payload.get('f3'))
    chat_ids = [item.get('f1') for item in feed_items if isinstance(item, dict) and item.get('f1')]
    print(f'[fetchRecentChatIds] got {len(chat_ids)} chatIds from feed', file=sys.stderr)
    return chat_ids


# ---------------------------------------------------------------------------
# search_active_groups_page
# ---------------------------------------------------------------------------

def search_active_groups_page(cookies, seq_id, session_id, pagination_token, recent_chat_ids=None):
    recent_chat_ids = recent_chat_ids or []
    chat_filter = {1: {2: 0}, 6: 2, 8: 1, 10: 1}
    if recent_chat_ids:
        chat_filter[7] = recent_chat_ids
    entity_items = [{1: 3, 2: {2: chat_filter}}, {1: 24}]
    search_request = {
        1: session_id, 2: seq_id, 3: ' ',
        5: {
            1: 'SEARCH_CHATS_IN_ADVANCE_SCENE',
            2: entity_items,
            3: {1: 1, 8: 1, 9: 1},
            4: {1: 6},
            6: {1: ' ', 2: 4},
        },
        6: 'zh_CN',
        8: {6: 1, 7: 1, 10: 0, 12: 1, 13: 1},
        9: {2: 200},
        10: {2: 202},
        16: 'Asia/Shanghai',
        18: '1',
    }
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
        return {'groups': [], 'hasMore': False, 'nextToken': None}

    items = to_array(isinstance(resp_payload, dict) and resp_payload.get('f2'))
    groups = []
    for item in items:
        if not isinstance(item, dict):
            continue
        last_msg_time = None
        f9items = to_array(isinstance(item.get('f9'), dict) and item['f9'].get('f1'))
        for f9item in f9items:
            if isinstance(f9item, dict) and f9item.get('f3') == '2' and f9item.get('f1'):
                last_msg_time = int(f9item['f1']) if str(f9item['f1']).isdigit() else None
        chat_meta = isinstance(item.get('f7'), dict) and isinstance(item['f7'].get('f3'), dict) and item['f7']['f3']
        member_count = None
        if chat_meta and isinstance(chat_meta, dict):
            raw = chat_meta.get('f11')
            if raw is not None:
                try:
                    member_count = int(raw)
                except (ValueError, TypeError):
                    pass
        groups.append({
            'chatId': item.get('f1', ''),
            'name': strip_highlight(item.get('f3', '')),
            'memberCount': member_count,
            'lastMsgTime': last_msg_time,
        })

    has_more, next_token = False, None
    try:
        f1 = isinstance(resp_payload, dict) and resp_payload.get('f1', {})
        info_str = (isinstance(f1, dict) and f1.get('f5')) or (isinstance(resp_payload, dict) and resp_payload.get('f5'))
        if isinstance(info_str, str) and info_str.startswith('{'):
            info = json.loads(info_str)
            has_more = bool(info.get('HasMore'))
            next_token = info_str
    except Exception:
        pass
    return {'groups': groups, 'hasMore': has_more, 'nextToken': next_token}


# ---------------------------------------------------------------------------
# search_group_member_count
# ---------------------------------------------------------------------------

def search_group_member_count(cookies, chat_name, chat_id):
    if not chat_name:
        return None
    import re
    query = chat_name
    m = re.search(r'[（(\[【]', query)
    if m and m.start() > 2:
        query = query[:m.start()].strip()
    if len(query) > 20:
        query = query[:20]

    session_id = _rand_session('mc')
    config = SEARCH_CONFIGS['groups']
    search_request = {
        1: session_id, 2: 1, 3: query,
        5: {1: config['tagName'], 2: config['entityItems'], 6: {1: query, 2: 0}},
        6: 'zh_CN', 15: 2, 16: 'Asia/Shanghai', 18: '1',
    }
    payload_bytes = encode_message({1: search_request})
    resp = send_gateway_request(cookies, 11021, payload_bytes)
    result = decode_response(resp['buffer'])
    packet = result.get('packet')
    resp_payload = result.get('payload')
    status = packet and packet.get('status', 0)
    if not packet or (status and str(status) != '0'):
        return None
    groups = format_search_groups(resp_payload)
    if chat_id:
        for g in groups:
            if g.get('chatId') == chat_id:
                return g.get('memberCount')
    for g in groups:
        if g.get('name') == chat_name:
            return g.get('memberCount')
    if groups:
        return groups[0].get('memberCount')
    return None


# ---------------------------------------------------------------------------
# get_chat_msg_count_hint — cheap upper bound on max_pos via cmd=64
# ---------------------------------------------------------------------------
# cmd=64 (PULL_CHATS_BY_IDS) returns chat metadata. Two observed schemas:
#   Schema A (most groups): entity.f15 ≈ max_pos (may overshoot by a few
#     because trailing call-ended/system events occupy positions that
#     cmd=58 silently filters). f15 = UINT64_MAX (2^64-1) is the server's
#     "0 messages" sentinel.
#   Schema B (some active community groups, e.g. with topic feature):
#     f15 is absent entirely. No other entity field reliably exposes
#     max_pos — f33/f107 is some "anchor msg_id" that lags true max
#     by 0–600 positions; f35/f103/f106 is the user's join_pos.
#     For schema B we fall back to a binary search via cmd=58 (~17 RTT,
#     same cost as the pre-perf-rewrite path).
#
# Callers should over-fetch by a small BUFFER and dedupe by position to
# absorb schema-A's off-by-a-few; cmd=58 silently skips empty positions, so
# overshooting `start_pos` is free.

_UINT64_MAX = 18446744073709551615


def _binary_search_max_pos(cookies, chat_id):
    """Binary search via cmd=58 for the true max_pos. Returns 0 for empty chat."""
    lo, hi = 0, 100000
    while lo < hi:
        mid = (lo + hi) // 2
        resp = send_gateway_request(cookies, 58, encode_message({1: chat_id, 2: mid, 3: 1}))
        payload = decode_response(resp['buffer']).get('payload')
        positions = to_array(isinstance(payload, dict) and payload.get('f1'))
        if positions:
            lo = mid + 1
        else:
            hi = mid
    return max(0, lo - 1)


def get_chat_msg_count_hint(cookies, chat_id):
    """Returns int hint (>=0), 0 for empty chat, or None on cmd=64 failure."""
    try:
        resp = send_gateway_request(cookies, 64, encode_message({1: [chat_id]}))
        payload = decode_response(resp['buffer']).get('payload')
        chats = to_array(isinstance(payload, dict) and payload.get('f1'))
        if not chats or not isinstance(chats[0], dict):
            return None
        entity = chats[0].get('f2') if isinstance(chats[0].get('f2'), dict) else chats[0]
        raw = entity.get('f15')
        if raw is not None and str(raw).isdigit():
            v = int(raw)
            return 0 if v == _UINT64_MAX else v
        # Schema B: no f15. Fall back to binary search via cmd=58.
        return _binary_search_max_pos(cookies, chat_id)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Topic-group helpers (entities.Chat.f39 == 3 → thread/channel model)
# ---------------------------------------------------------------------------

def fetch_chat_entity(cookies, chat_id):
    """Return the raw entities.Chat dict from cmd=64, or None on failure."""
    try:
        resp = send_gateway_request(cookies, 64, encode_message({1: [chat_id]}))
        payload = decode_response(resp['buffer']).get('payload')
        chats = to_array(isinstance(payload, dict) and payload.get('f1'))
        if not chats or not isinstance(chats[0], dict):
            return None
        entity = chats[0].get('f2') if isinstance(chats[0].get('f2'), dict) else chats[0]
        return entity if isinstance(entity, dict) else None
    except Exception:
        return None


def is_topic_group(cookies, chat_id):
    """True iff chat is a 话题群 (Chat.f39 == 3). False on any uncertainty."""
    entity = fetch_chat_entity(cookies, chat_id)
    return bool(entity) and str(entity.get('f39', '')) == '3'


def pull_topic_threads(cookies, chat_id, count=20, max_thread_pos=None):
    """Pull the latest `count` threads from a topic group via cmd=8108.

    Strategy: cmd=64 → entity.f35 = max thread position. Then cmd=8108 with
    positions=[max, max-1, ..., max-count+1] (batched as a single request).
    Server returns one Thread per anchor when present.

    If `max_thread_pos` is given, skip the cmd=64 lookup. Returns a list of
    raw Thread dicts (entities.Thread proto), sorted by Thread.position desc.
    """
    if max_thread_pos is None:
        entity = fetch_chat_entity(cookies, chat_id)
        if not entity:
            return []
        # f35 / f103 / f106 all hold the max thread position; prefer f35.
        raw = entity.get('f35') or entity.get('f103') or entity.get('f106')
        if not raw or not str(raw).isdigit():
            return []
        max_thread_pos = int(raw)
    if max_thread_pos < 1 or count < 1:
        return []
    # Build anchor list: latest `count` positions descending.
    positions = list(range(max_thread_pos, max(0, max_thread_pos - count), -1))
    req = {
        1: chat_id,
        2: 1,           # type = CHAT_CHANNEL
        3: positions,   # positions (anchors)
        4: True,        # onlyVisibleThreads
        5: 1,           # direction = UP
        10: 2,          # lastMessageCount per thread
    }
    resp = send_gateway_request(cookies, 8108, encode_message(req))
    payload = decode_response(resp['buffer']).get('payload') or {}
    raw_threads = payload.get('f1')
    if isinstance(raw_threads, dict):
        entries = [raw_threads]
    elif isinstance(raw_threads, list):
        entries = raw_threads
    else:
        entries = []
    # Each entry is a map<int32 position, Thread> wire entry: {f1: pos, f2: Thread}
    threads = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        pos_str = e.get('f1')
        body = e.get('f2')
        if not isinstance(body, dict):
            continue
        threads.append(body)
    # Sort by Thread.position (f5) desc — newest first
    threads.sort(key=lambda t: -int(t.get('f5', 0) or 0))
    return threads


def pull_thread_messages(cookies, chat_id, thread_id, anchor_pos=None, count=20):
    """Pull messages from a single thread via cmd=8109.

    `anchor_pos` is the message position within the thread to start from
    (use the thread's lastMessagePosition + a small buffer). When None,
    we batch-anchor positions [N, N-1, ..., 0] for small threads.

    Returns a list of raw Message dicts sorted by chat-level position desc
    (matches the existing普通群 message output).
    """
    # For thread messages: positions[] are anchor message positions within thread.
    # If anchor not given, fan-out from `count` down to 0 to cover small threads.
    if anchor_pos is None:
        positions = list(range(count, -1, -1))
    else:
        positions = list(range(anchor_pos, max(-1, anchor_pos - count), -1))
    req = {
        1: thread_id,
        2: positions,   # positions (anchors)
        3: True,        # onlyVisibleMessages
        4: 1,           # direction = UP
        9: chat_id,     # channelId
        10: 1,          # channelType = CHAT_CHANNEL
        11: True,       # needNextPosition
    }
    resp = send_gateway_request(cookies, 8109, encode_message(req))
    payload = decode_response(resp['buffer']).get('payload') or {}
    raw_msgs = payload.get('f1')
    if isinstance(raw_msgs, dict):
        entries = [raw_msgs]
    elif isinstance(raw_msgs, list):
        entries = raw_msgs
    else:
        entries = []
    # Return the raw map entries `{f1: pos, f2: Message}` — callers that need
    # to format with format_chat_messages() expect this shape.
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get('f2'), dict)]


# ---------------------------------------------------------------------------
# fetch_chat_messages_in_range
# ---------------------------------------------------------------------------

def fetch_chat_messages_in_range(cookies, chat_id, from_ts, to_ts, max_msgs=200):
    BATCH = 50
    MAX_ROUNDS = 20
    all_msgs = []

    # cmd=64 upper-bound hint; +5 BUFFER absorbs trailing call/system events
    hint = get_chat_msg_count_hint(cookies, chat_id)
    if hint is None or hint == 0:
        return []
    current_end = hint + 5
    for _ in range(MAX_ROUNDS):
        start_pos = max(0, current_end - BATCH + 1)
        fetch_count = current_end - start_pos + 1
        if fetch_count <= 0:
            break

        pos_resp = send_gateway_request(cookies, 58, encode_message({1: chat_id, 2: start_pos, 3: fetch_count}))
        pos_result = decode_response(pos_resp['buffer'])
        pos_payload = pos_result.get('payload')
        positions = to_array(isinstance(pos_payload, dict) and pos_payload.get('f1'))
        msg_ids = [p.get('f2') for p in positions if isinstance(p, dict) and p.get('f2')]
        if not msg_ids:
            break

        msg_resp = send_gateway_request(cookies, 8, encode_message({1: msg_ids}))
        msg_result = decode_response(msg_resp['buffer'])
        msg_payload = msg_result.get('payload')
        messages = format_chat_messages(msg_payload)

        has_older = False
        for msg in messages:
            ts = msg.get('timestamp')
            if ts and from_ts <= ts <= to_ts:
                all_msgs.append(msg)
            if ts and ts < from_ts:
                has_older = True

        if has_older or start_pos == 0 or len(all_msgs) >= max_msgs:
            break
        current_end = start_pos - 1

    all_msgs.sort(key=lambda m: m.get('timestamp') or 0)
    if len(all_msgs) > max_msgs:
        return all_msgs[len(all_msgs) - max_msgs:]
    return all_msgs


# ---------------------------------------------------------------------------
# cmd_chat_today
# ---------------------------------------------------------------------------

def cmd_chat_today(cookies, opts=None):
    opts = opts or {}
    no_bot = opts.get('noBot', True)
    limit = opts.get('limit', 1000)

    from datetime import datetime, timezone, timedelta
    shanghai_tz = timezone(timedelta(hours=8))

    if opts.get('from') and opts.get('to'):
        time_range = {'from': opts['from'], 'to': opts['to']}
        date_label = f"{datetime.fromtimestamp(opts['from'], tz=shanghai_tz).strftime('%Y-%m-%d')} ~ {datetime.fromtimestamp(opts['to'], tz=shanghai_tz).strftime('%Y-%m-%d')}"
    elif opts.get('week'):
        now_sh = datetime.now(shanghai_tz)
        dow = now_sh.weekday()  # 0=Mon
        monday = now_sh - timedelta(days=dow)
        from_ts = int(monday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        to_ts = int(datetime.now(timezone.utc).timestamp())
        time_range = {'from': from_ts, 'to': to_ts}
        date_label = f"week: {monday.strftime('%Y-%m-%d')} ~ {now_sh.strftime('%Y-%m-%d')}"
    else:
        tr = get_today_time_range()
        time_range = {'from': tr['from'], 'to': tr['to']}
        date_label = tr['dateStr']

    all_messages = {}   # messageId -> msg
    chat_groups = {}    # chatId -> group dict

    print(f"[chat search] Searching messages for {date_label} ({time_range['from']}~{time_range['to']})"
          f"{'[no-bot]' if no_bot else ''} limit={limit}...", file=sys.stderr)

    if opts.get('all'):
        # ===== Legacy flow: full scan (P2P + active group discovery) =====
        session_id = _rand_session('p2p')
        pagination_token = None
        p2p_max_pages = 10
        print('[chat search] Phase 1a: Searching P2P messages...', file=sys.stderr)
        for seq_id in range(1, p2p_max_pages + 1):
            page = search_messages_page(cookies, ' ', seq_id, time_range, session_id, pagination_token, no_bot, 'p2p')
            if not page['results']:
                break
            prev_size = len(all_messages)
            for msg in page['results']:
                all_messages[msg['messageId']] = msg
            new_count = len(all_messages) - prev_size
            print(f"[chat search] P2P page {seq_id}: {len(page['results'])} results, {new_count} new, total {len(all_messages)} unique", file=sys.stderr)
            pagination_token = page['nextToken']
            if not page['hasMore'] or new_count == 0:
                break

        recent_chat_ids = fetch_recent_chat_ids(cookies, 50)
        group_session = _rand_session('grp')
        group_token = None
        active_groups = []
        MAX_GROUP_PAGES = 30
        print(f"[chat search] Phase 1b: Searching active groups ({len(recent_chat_ids)} seed chatIds)...", file=sys.stderr)
        for seq_id in range(1, MAX_GROUP_PAGES + 1):
            page = search_active_groups_page(cookies, seq_id, group_session, group_token, recent_chat_ids)
            if not page['groups']:
                break
            stopped_early = False
            for g in page['groups']:
                if g['lastMsgTime'] and g['lastMsgTime'] >= time_range['from']:
                    active_groups.append(g)
                elif g['lastMsgTime'] and g['lastMsgTime'] < time_range['from']:
                    stopped_early = True
                    break
            print(f"[chat search] Group page {seq_id}: {len(page['groups'])} groups, {len(active_groups)} active so far", file=sys.stderr)
            group_token = page['nextToken']
            if stopped_early or not page['hasMore']:
                break
        print(f"[chat search] Found {len(active_groups)} groups with messages in range", file=sys.stderr)

        for msg in all_messages.values():
            cid = msg.get('chatId') or 'unknown'
            if cid not in chat_groups:
                chat_groups[cid] = {'chatName': msg.get('chatName'), 'chatId': cid, 'isP2P': msg.get('isP2P'), 'memberCount': 2, 'messages': []}
            slim = {k: v for k, v in msg.items() if k not in ('chatName', 'chatId', 'isP2P')}
            chat_groups[cid]['messages'].append(slim)
        for g in active_groups:
            if g['chatId'] not in chat_groups:
                chat_groups[g['chatId']] = {'chatName': g['name'], 'chatId': g['chatId'], 'isP2P': False, 'memberCount': g['memberCount'], 'messages': []}
            else:
                chat_groups[g['chatId']]['memberCount'] = g['memberCount']

    else:
        # ===== Default flow: "from me" + "@me" =====
        user_id = get_current_user_id(cookies)
        if not user_id:
            print('[chat search] ERROR: Cannot determine current userId.', file=sys.stderr)
            print(json.dumps({'error': 'NO_USER_ID', 'message': 'Cannot determine current userId'}))
            return
        print(f'[chat search] userId={user_id}, using mine-only mode', file=sys.stderr)

        from_me_chat_ids = set()
        from_me_session = _rand_session('fm')
        from_me_token = None
        FROM_ME_MAX = 15
        print('[chat search] Phase 1a: Searching messages FROM me...', file=sys.stderr)
        for seq_id in range(1, FROM_ME_MAX + 1):
            page = search_messages_page(cookies, ' ', seq_id, time_range, from_me_session, from_me_token, no_bot, None, sender_user_id=user_id)
            if not page['results']:
                break
            prev_size = len(all_messages)
            for msg in page['results']:
                all_messages[msg['messageId']] = msg
                if msg.get('chatId'):
                    from_me_chat_ids.add(msg['chatId'])
            new_count = len(all_messages) - prev_size
            print(f"[chat search] from-me page {seq_id}: {len(page['results'])} results, {new_count} new, total {len(all_messages)} unique", file=sys.stderr)
            from_me_token = page['nextToken']
            if not page['hasMore'] or new_count == 0 or len(all_messages) >= limit:
                break

        at_me_session = _rand_session('am')
        at_me_token = None
        AT_ME_MAX = 15
        print('[chat search] Phase 1b: Searching messages @me...', file=sys.stderr)
        for seq_id in range(1, AT_ME_MAX + 1):
            page = search_messages_page(cookies, ' ', seq_id, time_range, at_me_session, at_me_token, no_bot, None, at_user_id=user_id)
            if not page['results']:
                break
            prev_size = len(all_messages)
            for msg in page['results']:
                all_messages[msg['messageId']] = msg
            new_count = len(all_messages) - prev_size
            print(f"[chat search] @me page {seq_id}: {len(page['results'])} results, {new_count} new, total {len(all_messages)} unique", file=sys.stderr)
            at_me_token = page['nextToken']
            if not page['hasMore'] or new_count == 0 or len(all_messages) >= limit:
                break

        user_id_pattern = f'user_id="{user_id}"'
        filtered_count = 0
        for msg_id in list(all_messages.keys()):
            msg = all_messages[msg_id]
            cid = msg.get('chatId')
            if not cid or msg.get('isP2P') or cid in from_me_chat_ids:
                continue
            if user_id_pattern not in (msg.get('content') or ''):
                del all_messages[msg_id]
                filtered_count += 1
        if filtered_count:
            print(f'[chat search] Filtered {filtered_count} @all messages', file=sys.stderr)

        unique_chats = len(set(m.get('chatId') for m in all_messages.values()))
        print(f'[chat search] Phase 1 done: {len(all_messages)} unique messages from {unique_chats} chats', file=sys.stderr)

        for msg in all_messages.values():
            cid = msg.get('chatId') or 'unknown'
            if cid not in chat_groups:
                chat_groups[cid] = {'chatName': msg.get('chatName'), 'chatId': cid, 'isP2P': msg.get('isP2P'), 'memberCount': 2 if msg.get('isP2P') else None, 'messages': []}
            slim = {k: v for k, v in msg.items() if k not in ('chatName', 'chatId', 'isP2P')}
            chat_groups[cid]['messages'].append(slim)

    chat_ids = [cid for cid in chat_groups if cid != 'unknown']
    MEMBER_THRESHOLD = opts.get('memberThreshold', 30)
    full_load_ids = [cid for cid in chat_ids if chat_groups[cid].get('isP2P') or (chat_groups[cid].get('memberCount') or 0) <= MEMBER_THRESHOLD]
    summary_ids = [cid for cid in chat_ids if cid not in full_load_ids]
    print(f"[chat search] Strategy: {len(full_load_ids)} chats full-load (≤{MEMBER_THRESHOLD} members), {len(summary_ids)} chats summary-only", file=sys.stderr)

    # Compute per-chat fetch windows: ±12h around Phase 1 message timestamps, clamped to query range
    WINDOW_SEC = 12 * 3600
    chat_fetch_ranges = {}
    for cid in full_load_ids:
        timestamps = [m['timestamp'] for m in chat_groups[cid]['messages'] if m.get('timestamp')]
        if timestamps:
            fr = max(min(timestamps) - WINDOW_SEC, time_range['from'])
            to = min(max(timestamps) + WINDOW_SEC, time_range['to'])
        else:
            fr, to = time_range['from'], time_range['to']
        chat_fetch_ranges[cid] = (fr, to)

    if full_load_ids:
        print(f'[chat search] Fetching full messages for {len(full_load_ids)} chats...', file=sys.stderr)
        CONCURRENCY = 8
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
            futures = {executor.submit(fetch_chat_messages_in_range, cookies, cid, *chat_fetch_ranges[cid]): cid for cid in full_load_ids}
            for future in futures:
                cid = futures[future]
                try:
                    full_msgs = future.result()
                    if full_msgs:
                        prev = len(chat_groups[cid]['messages'])
                        chat_groups[cid]['messages'] = full_msgs
                        print(f"[chat search] {chat_groups[cid]['chatName']}: {prev} → {len(full_msgs)} messages", file=sys.stderr)
                except Exception:
                    pass

    all_sender_ids = set()
    for cid in full_load_ids:
        for msg in chat_groups[cid]['messages']:
            if msg.get('senderId') and not msg.get('sender'):
                all_sender_ids.add(msg['senderId'])
    if all_sender_ids:
        user_map = batch_resolve_user_names(cookies, list(all_sender_ids))
        print(f"[chat search] Resolved {len(user_map)}/{len(all_sender_ids)} sender names", file=sys.stderr)
        for cid in full_load_ids:
            for msg in chat_groups[cid]['messages']:
                if msg.get('senderId') and not msg.get('sender') and msg['senderId'] in user_map:
                    msg['sender'] = user_map[msg['senderId']]

    for cid in summary_ids:
        g = chat_groups[cid]
        g['messageCount'] = len(g['messages'])
        g['messages'] = []
        g['summaryOnly'] = True
        print(f"[chat search] {g['chatName']} ({g.get('memberCount')} members): summary only", file=sys.stderr)

    for g in chat_groups.values():
        g['messages'].sort(key=lambda m: m.get('timestamp') or 0)

    chats = sorted(chat_groups.values(), key=lambda g: (
        1 if g.get('summaryOnly') else 0,
        -(g['messages'][-1].get('timestamp') or 0) if not g.get('summaryOnly') and g['messages'] else 0,
        g.get('memberCount') or 0 if g.get('summaryOnly') else 0,
    ))

    if not opts.get('verbose'):
        for chat in chats:
            chat['messages'] = [_project_message_default(m) for m in chat['messages']]

    total_full = sum(len(c['messages']) for c in chats if not c.get('summaryOnly'))
    total_summary = sum(1 for c in chats if c.get('summaryOnly'))

    if opts.get('compact'):
        print(f"# {date_label} ({total_full} messages, {len(chats)} chats)")
        for chat in chats:
            if not chat['messages'] and not chat.get('summaryOnly'):
                continue
            print(f"\n## {chat['chatName']}")
            if chat.get('summaryOnly'):
                print(f"({chat.get('memberCount')} members, {chat.get('messageCount')} messages, summary only)")
            else:
                for m in chat['messages']:
                    time_str = m.get('time') or '?'
                    print(f"[{time_str}] {m.get('sender') or '?'}: {m.get('content')}")
        return

    print(json.dumps({
        'success': True,
        'date': date_label,
        'totalFullMessages': total_full,
        'totalChats': len(chats),
        'fullLoadChats': len(chats) - total_summary,
        'summaryOnlyChats': total_summary,
        'chats': chats,
    }))


# ---------------------------------------------------------------------------
# Topic-group thread list emission (called from cmd_chat_messages)
# ---------------------------------------------------------------------------

def _emit_topic_threads(cookies, chat_id, entity, count, opts, raw=False):
    """Pull the latest `count` threads in a topic group and render them.

    Output shape mirrors the普通群 result style but with `kind=topic_group`
    and a `threads` array instead of `messages`.
    """
    raw_max = entity.get('f35') or entity.get('f103') or entity.get('f106')
    if not raw_max or not str(raw_max).isdigit() or int(raw_max) < 1:
        if opts.get('compact'):
            print('(no threads)')
            return
        print(json.dumps({'success': True, 'chatId': chat_id, 'kind': 'topic_group',
                          'count': 0, 'threads': []}))
        return

    threads_raw = pull_topic_threads(cookies, chat_id, count=count,
                                     max_thread_pos=int(raw_max))
    if raw:
        print(json.dumps({'success': True, 'chatId': chat_id, 'kind': 'topic_group',
                          'maxThreadPos': int(raw_max), 'threadsRaw': threads_raw}))
        return

    # Resolve owner names in one batch
    owner_ids = list({str(t.get('f14')) for t in threads_raw if t.get('f14')})
    user_map = batch_resolve_user_names(cookies, owner_ids) if owner_ids else {}

    threads = []
    for t in threads_raw:
        owner_id = str(t.get('f14') or '')
        update_ts_raw = t.get('f6')
        try:
            update_ts = int(update_ts_raw) if update_ts_raw else 0
        except (TypeError, ValueError):
            update_ts = 0
        try:
            create_ts = int(t.get('f23')) if t.get('f23') else 0
        except (TypeError, ValueError):
            create_ts = 0
        try:
            reply_count = int(t.get('f11', 0))
        except (TypeError, ValueError):
            reply_count = 0
        try:
            position = int(t.get('f5', 0))
        except (TypeError, ValueError):
            position = 0
        threads.append({
            'thread_id': t.get('f1'),
            'topic': t.get('f4') or '',
            'subtitle': t.get('f24') or '',
            'position': position,
            'reply_count': reply_count,
            'owner_id': owner_id,
            'owner_name': user_map.get(owner_id) or owner_id,
            'create_time': create_ts,
            'update_time': update_ts,
            'last_message_id': t.get('f8'),
            'time': time.strftime('%Y/%m/%d %H:%M:%S', time.localtime(update_ts)) if update_ts else None,
        })

    if opts.get('compact'):
        print(f'[topic-group {chat_id}] {len(threads)} threads:')
        for th in threads:
            t_label = th['topic'] or '(无主题)'
            print(f"[{th.get('time') or '?'}] {th['owner_name']} (pos={th['position']}, {th['reply_count']} replies)")
            print(f"  └ {t_label}: {th['subtitle']}")
        return

    print(json.dumps({
        'success': True,
        'chatId': chat_id,
        'kind': 'topic_group',
        'count': len(threads),
        'threads': threads,
    }, ensure_ascii=False))


# ---------------------------------------------------------------------------
# cmd_chat_messages
# ---------------------------------------------------------------------------

def cmd_chat_messages(cookies, chat_id, raw=False, opts=None):
    opts = opts or {}
    count = opts.get('count', 20)
    before = opts.get('before')

    if before is not None:
        start_pos = max(0, before - count)
        pos_resp = send_gateway_request(cookies, 58, encode_message({1: chat_id, 2: start_pos, 3: count}))
        pos_payload = decode_response(pos_resp['buffer']).get('payload')
        positions = to_array(isinstance(pos_payload, dict) and pos_payload.get('f1'))
    else:
        # Fetch chat entity once for type detection + msg-count hint
        entity = fetch_chat_entity(cookies, chat_id)
        if entity is None:
            if opts.get('compact'):
                print('(metadata fetch failed)')
                return
            print(json.dumps({'success': False, 'error': 'METADATA_FETCH_FAILED', 'chatId': chat_id}))
            return

        # Topic group (话题群) → emit thread list and return early
        if str(entity.get('f39', '')) == '3':
            _emit_topic_threads(cookies, chat_id, entity, count, opts, raw=raw)
            return

        # 普通群 / P2P path: cmd=64 hint + cmd=58 loop downward to absorb gaps
        BATCH = 50
        MAX_ROUNDS = 20
        BUFFER = 5

        f15 = entity.get('f15')
        if f15 is not None and str(f15).isdigit():
            v = int(f15)
            hint = 0 if v == _UINT64_MAX else v
        else:
            # Schema B (lacks f15) — fall back to binary search via cmd=58
            hint = _binary_search_max_pos(cookies, chat_id)
        if hint == 0:
            if opts.get('compact'):
                print('(no messages)')
                return
            print(json.dumps({'success': True, 'chatId': chat_id, 'count': 0, 'messages': []}))
            return

        accumulated = {}  # position(int) -> raw entry dict
        current_end = hint + BUFFER
        pos_payload = None
        for _ in range(MAX_ROUNDS):
            sp = max(0, current_end - BATCH + 1)
            fc = current_end - sp + 1
            if fc <= 0:
                break
            pr = send_gateway_request(cookies, 58, encode_message({1: chat_id, 2: sp, 3: fc}))
            pos_payload = decode_response(pr['buffer']).get('payload')
            ps = to_array(isinstance(pos_payload, dict) and pos_payload.get('f1'))
            for p in ps:
                if isinstance(p, dict) and p.get('f2') and str(p.get('f1', '')).isdigit():
                    accumulated[int(p['f1'])] = p
            if len(accumulated) >= count or sp == 0:
                break
            current_end = sp - 1

        if not accumulated:
            if opts.get('compact'):
                print('(no messages)')
                return
            print(json.dumps({'success': True, 'chatId': chat_id, 'count': 0, 'messages': []}))
            return

        top_positions = sorted(accumulated.keys(), reverse=True)[:count]
        positions = [accumulated[p] for p in top_positions]
        start_pos = min(top_positions)

    msg_ids = [p.get('f2') for p in positions if isinstance(p, dict) and p.get('f2')]

    if not msg_ids:
        if opts.get('compact'):
            print('(no messages)')
            return
        print(json.dumps({'success': True, 'chatId': chat_id, 'count': 0, 'messages': []}))
        return

    msg_resp = send_gateway_request(cookies, 8, encode_message({1: msg_ids}))
    msg_result = decode_response(msg_resp['buffer'])
    packet = msg_result.get('packet')
    payload = msg_result.get('payload')
    status = packet and packet.get('status', 0)
    success = packet is not None and (not status or status == 0 or str(status) == '0')

    if raw:
        print(json.dumps({'success': success, 'chatId': chat_id, 'startPos': start_pos, 'posPayload': pos_payload, 'payload': payload}))
        return

    items = to_array(isinstance(payload, dict) and payload.get('f1'))
    sender_ids = list({
        (item.get('f2') or item).get('f3')
        for item in items
        if isinstance(item, dict) and isinstance((item.get('f2') or item), dict) and (item.get('f2') or item).get('f3')
    })
    user_map = batch_resolve_user_names(cookies, sender_ids) if sender_ids else {}

    format_opts = {'includeHtml': opts.get('html'), 'verbose': opts.get('verbose'), 'userMap': user_map}
    messages = format_chat_messages(payload, format_opts)
    messages.sort(key=lambda m: m.get('timestamp') or 0)

    if opts.get('compact'):
        for m in messages:
            time_str = m.get('time') or '?'
            print(f"[{time_str}] {m.get('sender') or '?'}: {m.get('content')}")
        return

    if not opts.get('verbose'):
        messages = [_project_message_default(m) for m in messages]

    pos_nums = [int(p.get('f1')) for p in positions if isinstance(p, dict) and str(p.get('f1', '')).isdigit()]
    first_position = min(pos_nums) if pos_nums else None
    print(json.dumps({'success': success, 'chatId': chat_id, 'count': len(messages), 'firstPosition': first_position, 'messages': messages}))


# ---------------------------------------------------------------------------
# cmd_chat_thread — drill into a single thread (话题群 only)
# ---------------------------------------------------------------------------

def cmd_chat_thread(cookies, chat_id, thread_id, raw=False, opts=None):
    """Pull messages within a single topic-group thread via cmd=8109."""
    opts = opts or {}
    count = opts.get('count', 20)

    entries = pull_thread_messages(cookies, chat_id, thread_id, count=count)
    if not entries:
        if opts.get('compact'):
            print('(no messages)')
            return
        print(json.dumps({'success': True, 'chatId': chat_id, 'threadId': thread_id,
                          'count': 0, 'messages': []}))
        return

    if raw:
        print(json.dumps({'success': True, 'chatId': chat_id, 'threadId': thread_id,
                          'rawEntries': entries}))
        return

    # Each entry is `{f1: pos, f2: Message}`. format_chat_messages handles this shape.
    sender_ids = list({e['f2'].get('f3') for e in entries if e.get('f2', {}).get('f3')})
    user_map = batch_resolve_user_names(cookies, sender_ids) if sender_ids else {}
    format_opts = {'includeHtml': opts.get('html'), 'verbose': opts.get('verbose'), 'userMap': user_map}
    messages = format_chat_messages({'f1': entries}, format_opts)
    messages.sort(key=lambda m: m.get('timestamp') or 0)

    if opts.get('compact'):
        for m in messages:
            time_str = m.get('time') or '?'
            print(f"[{time_str}] {m.get('sender') or '?'}: {m.get('content')}")
        return

    if not opts.get('verbose'):
        messages = [_project_message_default(m) for m in messages]

    print(json.dumps({'success': True, 'chatId': chat_id, 'threadId': thread_id,
                      'count': len(messages), 'messages': messages}, ensure_ascii=False))


# ---------------------------------------------------------------------------
# cmd_chat_p2p
# ---------------------------------------------------------------------------

def cmd_chat_p2p(cookies, user_id, raw=False):
    resp = send_gateway_request(cookies, 5030, encode_message({1: user_id}))
    result = decode_response(resp['buffer'])
    packet = result.get('packet')
    payload = result.get('payload')
    status = packet and packet.get('status', 0)
    success = packet is not None and (not status or status == 0 or str(status) == '0')
    if raw:
        print(json.dumps({'success': success, 'userId': user_id, 'payload': payload}))
        return
    chat_id = isinstance(payload, dict) and isinstance(payload.get('f2'), dict) and payload['f2'].get('f2')
    exists = isinstance(payload, dict) and isinstance(payload.get('f1'), dict) and payload['f1'].get('f2') == '1'
    print(json.dumps({'success': success, 'userId': user_id, 'exists': bool(exists), 'chatId': chat_id or None}))


# ---------------------------------------------------------------------------
# cmd_chat_members  — cmd=44 PULL_CHAT_CHATTERS
# ---------------------------------------------------------------------------

_CHATTER_TYPE_LABELS = {
    '1': 'user',
    '2': 'bot',
}


def _chatter_type_label(raw):
    if raw is None or raw == '':
        return 'user'
    return _CHATTER_TYPE_LABELS.get(str(raw), f'type_{raw}')


def _fmt_join_time(ts):
    if not ts:
        return ''
    try:
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(int(ts), tz).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def cmd_chat_members(cookies, chat_id, raw=False, opts=None):
    """Pull group members via cmd=44 PULL_CHAT_CHATTERS (paginated)."""
    opts = opts or {}
    page_size = opts.get('pageSize', 200)
    max_total = opts.get('max')

    all_chatters = {}       # user_id -> Chatter dict
    group_nicknames = {}    # user_id -> nickname
    join_times = {}         # user_id -> unix seconds
    cursor = 0
    pages = 0
    success = True

    while True:
        req = {1: chat_id, 2: True, 5: page_size}
        if cursor:
            req[3] = cursor
        resp = send_gateway_request(cookies, 44, encode_message(req))
        result = decode_response(resp['buffer'])
        packet = result.get('packet')
        payload = result.get('payload')
        status = packet and packet.get('status', 0)
        ok = packet is not None and (not status or status == 0 or str(status) == '0')
        if not ok:
            success = False
            if raw:
                print(json.dumps({'success': False, 'chatId': chat_id, 'status': status, 'payload': payload}))
                return
            break

        if raw:
            print(json.dumps({'success': True, 'chatId': chat_id, 'page': pages, 'payload': payload}))
            # raw mode dumps per-page; continue paginating so user can see everything
            pass

        if isinstance(payload, dict):
            # chatters: map<string, Chatter> — wire form is repeated {f1: key, f2: value}
            for entry in to_array(payload.get('f1')):
                if not isinstance(entry, dict):
                    continue
                uid = entry.get('f1')
                chatter = entry.get('f2')
                if uid and isinstance(chatter, dict):
                    all_chatters[str(uid)] = chatter
            # groupNicknames: map<int64, string>
            for entry in to_array(payload.get('f7')):
                if isinstance(entry, dict) and entry.get('f1') is not None:
                    group_nicknames[str(entry['f1'])] = entry.get('f2') or ''
            # joinTimesSec: map<string, int64>
            for entry in to_array(payload.get('f12')):
                if isinstance(entry, dict) and entry.get('f1') is not None:
                    val = entry.get('f2')
                    if str(val).isdigit():
                        join_times[str(entry['f1'])] = int(val)

        pages += 1
        next_raw = isinstance(payload, dict) and payload.get('f5')
        next_cursor = int(next_raw) if next_raw and str(next_raw).isdigit() else 0
        if not next_cursor:
            break
        if max_total and len(all_chatters) >= max_total:
            break
        cursor = next_cursor

    if raw:
        return

    members = []
    for uid, chatter in all_chatters.items():
        members.append({
            'userId': uid,
            'name': chatter.get('f2', '') or '',
            'enUsName': chatter.get('f14', '') or '',
            'nickname': group_nicknames.get(uid, ''),
            'email': chatter.get('f25', '') or chatter.get('f35', '') or '',
            'type': _chatter_type_label(chatter.get('f9')),
            'typeRaw': chatter.get('f9'),
            'avatarUrl': chatter.get('f5', '') or '',
            'joinTime': _fmt_join_time(join_times.get(uid)),
            'joinTimestamp': join_times.get(uid),
        })

    members.sort(key=lambda m: (m.get('type') != 'user', (m.get('name') or '').lower()))

    print(json.dumps({
        'success': success,
        'chatId': chat_id,
        'count': len(members),
        'pages': pages,
        'members': members,
    }, ensure_ascii=False))
