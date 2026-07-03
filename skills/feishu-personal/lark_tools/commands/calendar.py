"""
commands/calendar.py - Calendar commands (mirrors lib/commands/calendar.js)
"""

import json
import math
import sys
from datetime import datetime, timezone, timedelta

from ..proto import encode_message
from ..gateway import send_gateway_request, decode_response


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

_SH = timezone(timedelta(hours=8))


def _format_time(unix):
    if not unix:
        return None
    try:
        return datetime.fromtimestamp(int(unix), tz=_SH).strftime('%Y/%m/%d %H:%M:%S')
    except Exception:
        return None


def _format_date(unix):
    if not unix:
        return None
    try:
        return datetime.fromtimestamp(int(unix), tz=_SH).strftime('%Y/%m/%d')
    except Exception:
        return None


def get_this_week_range() -> dict:
    now_sh = datetime.now(_SH)
    dow = now_sh.weekday()  # 0=Mon
    monday = now_sh - timedelta(days=dow)
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    sunday = monday + timedelta(days=7)
    return {
        'from': int(monday.astimezone(timezone.utc).timestamp()),
        'to': int(sunday.astimezone(timezone.utc).timestamp()),
    }


def get_today_range() -> dict:
    now_sh = datetime.now(_SH)
    start = now_sh.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return {
        'from': int(start.astimezone(timezone.utc).timestamp()),
        'to': int(end.astimezone(timezone.utc).timestamp()),
    }


# ---------------------------------------------------------------------------
# Gateway calls
# ---------------------------------------------------------------------------

def fetch_calendar_settings(cookies):
    resp = send_gateway_request(cookies, 1000041, b'')
    result = decode_response(resp['buffer'])
    return result.get('payload')


def fetch_calendar_list(cookies) -> list:
    calendars = []
    page_token = ''
    while True:
        msg = encode_message({1: page_token, 2: 200})
        resp = send_gateway_request(cookies, 1001001, msg)
        result = decode_response(resp['buffer'])
        payload = result.get('payload') or {}
        items = payload.get('f1') if isinstance(payload, dict) else None
        if items:
            arr = items if isinstance(items, list) else [items]
            calendars.extend(arr)
        next_token = isinstance(payload, dict) and payload.get('f2')
        has_more = isinstance(payload, dict) and payload.get('f3')
        if not next_token or has_more == '0' or not has_more:
            break
        page_token = next_token
    return calendars


def fetch_calendar_events(cookies, calendar_id: str, from_ts: int, to_ts: int) -> list:
    msg = encode_message({1: {1: calendar_id, 2: from_ts, 3: to_ts, 5: 100}})
    resp = send_gateway_request(cookies, 1002001, msg)
    result = decode_response(resp['buffer'])
    payload = result.get('payload') or {}
    events = None
    if isinstance(payload, dict):
        f1 = payload.get('f1')
        if isinstance(f1, dict):
            f2 = f1.get('f2')
            if isinstance(f2, dict):
                events = f2.get('f1')
    if not events:
        return []
    return events if isinstance(events, list) else [events]


def fetch_event_detail(cookies, event_id: str):
    msg = encode_message({1: event_id})
    resp = send_gateway_request(cookies, 1002006, msg)
    result = decode_response(resp['buffer'])
    payload = result.get('payload') or {}
    if isinstance(payload, dict):
        f1 = payload.get('f1')
        if isinstance(f1, dict):
            return f1.get('f2') or f1
        return f1
    return payload


# ---------------------------------------------------------------------------
# Recurring event instance time computation
# ---------------------------------------------------------------------------

def _compute_instance_times(ev: dict, query_from: int, query_to: int) -> dict:
    orig_start = int(ev.get('f25', 0))
    orig_end = int(ev.get('f27', 0))
    duration = orig_end - orig_start
    rrule = ev.get('f20')

    if not rrule or not query_from:
        return {'start': orig_start, 'end': orig_end}

    import re
    freq_m = re.search(r'FREQ=(\w+)', rrule)
    interval_m = re.search(r'INTERVAL=(\d+)', rrule)
    freq = freq_m.group(1) if freq_m else None
    interval = int(interval_m.group(1)) if interval_m else 1

    steps = {'DAILY': 86400, 'WEEKLY': 86400 * 7, 'MONTHLY': 86400 * 30, 'YEARLY': 86400 * 365}
    step = steps.get(freq)
    if not step:
        return {'start': orig_start, 'end': orig_end}
    step *= interval

    if query_from <= orig_start < query_to:
        return {'start': orig_start, 'end': orig_end}

    elapsed = query_from - orig_start
    periods = math.floor(elapsed / step)
    for p in range(periods - 1, periods + 3):
        if p < 0:
            continue
        inst_start = orig_start + p * step
        inst_end = inst_start + duration
        if query_from <= inst_start < query_to:
            return {'start': inst_start, 'end': inst_end}

    next_p = periods + 1
    return {'start': orig_start + next_p * step, 'end': orig_start + next_p * step + duration}


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _is_room(a: dict) -> bool:
    if not isinstance(a, dict):
        return False
    if a.get('f8') == '5':
        return True
    f100 = a.get('f100') or {}
    if isinstance(f100, dict):
        f3 = f100.get('f3') or {}
        if isinstance(f3, dict) and f3.get('f2') == 'INDIVIDUAL':
            return False
    name = a.get('f4') or ''
    import re
    return bool(re.search(r'[#栋楼F层]', name) and re.search(r'\(\d+\)', name))


def _clean_str(s):
    if not s or s in ('<bytes:0>', '<bytes:2>'):
        return None
    return s


def format_calendar(cal: dict) -> dict:
    return {
        'calendarId': cal.get('f1'),
        'name': cal.get('f15'),
        'timezone': _clean_str(cal.get('f3')),
        'type': cal.get('f5'),
        'eventCount': int(cal['f14']) if cal.get('f14') else None,
    }


def format_event(ev: dict, query_from: int = None, query_to: int = None) -> dict:
    if ev.get('f20'):
        times = _compute_instance_times(ev, query_from or 0, query_to or 0)
    else:
        times = {'start': int(ev.get('f25', 0)), 'end': int(ev.get('f27', 0))}

    result = {
        'eventId': ev.get('f1'),
        'title': ev.get('f29') or '(无标题)',
        'startTime': _format_time(times['start']),
        'endTime': _format_time(times['end']),
        'startUnix': times['start'],
        'endUnix': times['end'],
        'timezone': ev.get('f26') or 'Asia/Shanghai',
    }

    if _clean_str(ev.get('f20')):
        result['recurrence'] = ev['f20']

    f37 = ev.get('f37')
    if isinstance(f37, dict):
        result['organizer'] = f37.get('f4')

    f33 = ev.get('f33')
    if f33:
        all_a = f33 if isinstance(f33, list) else [f33]
        rooms = [a.get('f4') for a in all_a if isinstance(a, dict) and _is_room(a) and a.get('f4')]
        if rooms:
            result['location'] = ', '.join(rooms)

    if ev.get('f50'):
        result['hasVC'] = True

    return result


def format_event_detail(ev: dict) -> dict:
    base = format_event(ev)

    f30 = ev.get('f30')
    if f30 and f30 != '<bytes:0>':
        base['description'] = f30

    f33 = ev.get('f33')
    if f33:
        all_a = f33 if isinstance(f33, list) else [f33]
        status_map = {'0': 'pending', '1': 'accepted', '2': 'declined', '3': 'tentative'}
        attendees = []
        for a in all_a:
            if not isinstance(a, dict) or _is_room(a):
                continue
            entry = {'name': a.get('f4')}
            if a.get('f5'):
                entry['userId'] = a['f5']
            if a.get('f6') is not None:
                entry['status'] = status_map.get(str(a['f6']), a['f6'])
            f100 = a.get('f100') or {}
            atype = (isinstance(f100, dict) and isinstance(f100.get('f3'), dict) and f100['f3'].get('f2'))
            if atype == 'INDIVIDUAL':
                entry['type'] = 'person'
            elif not a.get('f8') and not a.get('f5'):
                entry['type'] = 'group'
            if entry.get('name'):
                attendees.append(entry)
        base['attendees'] = attendees

    f50 = ev.get('f50')
    if isinstance(f50, dict) and f50.get('f1'):
        base['meetingNo'] = f50['f1']

    return base


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_calendar_list(cookies, raw=False):
    print('[calendar] Fetching calendar list...', file=sys.stderr)
    settings = fetch_calendar_settings(cookies)
    calendars = fetch_calendar_list(cookies)

    if raw:
        print(json.dumps({'success': True, 'settings': settings, 'calendars': calendars}))
        return

    primary_id = None
    if isinstance(settings, dict):
        f1 = settings.get('f1')
        if isinstance(f1, dict):
            primary_id = f1.get('f1')

    formatted = []
    for c in calendars:
        if not isinstance(c, dict):
            continue
        fc = format_calendar(c)
        if fc['calendarId'] == primary_id:
            fc['isPrimary'] = True
        formatted.append(fc)

    print(json.dumps({'success': True, 'primaryCalendarId': primary_id, 'calendars': formatted}))


def cmd_calendar_events(cookies, raw=False, opts=None):
    opts = opts or {}
    now_sh = datetime.now(_SH)

    if opts.get('from') and opts.get('to'):
        from_ts, to_ts = opts['from'], opts['to']
        range_label = f"{_format_date(from_ts)} ~ {_format_date(to_ts)}"
    elif opts.get('today'):
        r = get_today_range()
        from_ts, to_ts = r['from'], r['to']
        range_label = _format_date(from_ts)
    elif opts.get('month'):
        month_start = now_sh.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if month_start.month == 12:
            month_end = month_start.replace(year=month_start.year + 1, month=1)
        else:
            month_end = month_start.replace(month=month_start.month + 1)
        from_ts = int(month_start.astimezone(timezone.utc).timestamp())
        to_ts = int(month_end.astimezone(timezone.utc).timestamp())
        range_label = f"{now_sh.year}年{now_sh.month}月"
    else:
        r = get_this_week_range()
        from_ts, to_ts = r['from'], r['to']
        range_label = f"{_format_date(from_ts)} ~ {_format_date(to_ts)}"

    print(f'[calendar] Fetching events for {range_label}...', file=sys.stderr)

    settings = fetch_calendar_settings(cookies)
    calendar_id = None
    if isinstance(settings, dict):
        f1 = settings.get('f1')
        if isinstance(f1, dict):
            calendar_id = f1.get('f1')

    if not calendar_id:
        print(json.dumps({'error': 'Could not get calendar ID'}))
        import sys as _sys
        _sys.exit(1)

    events = fetch_calendar_events(cookies, calendar_id, from_ts, to_ts)

    if raw:
        print(json.dumps({'success': True, 'calendarId': calendar_id, 'from': from_ts, 'to': to_ts, 'events': events}))
        return

    formatted = [format_event(ev, from_ts, to_ts) for ev in events if isinstance(ev, dict)]
    formatted.sort(key=lambda e: e.get('startUnix') or 0)

    print(json.dumps({'success': True, 'range': range_label, 'total': len(formatted), 'events': formatted}))


def cmd_calendar_detail(cookies, event_id: str, raw=False):
    print(f'[calendar] Fetching event {event_id}...', file=sys.stderr)
    detail = fetch_event_detail(cookies, event_id)

    if raw:
        print(json.dumps({'success': True, 'event': detail}))
        return

    print(json.dumps({'success': True, 'event': format_event_detail(detail) if isinstance(detail, dict) else detail}))
