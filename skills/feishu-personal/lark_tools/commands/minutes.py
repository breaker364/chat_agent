"""
commands/minutes.py - Feishu Minutes (妙记) read commands

Sub-commands:
  meta        — 元信息（参会人 + 说话人 + 状态 + 视频 URL）
  transcript  — 完整文字记录 Markdown（拼接段落正文 + 说话人 + 时间戳）
  summary     — 智能记要（按说话人聚合）
  chapters    — 会议纪要 + 章节纪要 + 关键讨论（从 SSR HTML 解析）
  download    — 整篇打包成 Markdown 落盘
  list        — 列最近的妙记（timestamp 游标翻页）
  search      — 关键词检索，含命中的 transcript/章节 snippet（offset 翻页）

API 来源：
  REST  : /minutes/api/status, /participants, /speakers, /summaries_v2,
          /subtitles/paragraph-ids, /subtitles_v2,
          /space/list (列表), /search (关键词)
  SSR HTML inline: 会议纪要 (section_id=18) / 章节纪要 (section_id=17) /
                   关键讨论 (section_id=19) 嵌在 /minutes/<token> 主页 HTML 里。
"""

import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import quote as urlquote, urlparse

from ..http_utils import http_get
from ..config import DOC_HOST


# ---------------------------------------------------------------------------
# Token / URL resolution
# ---------------------------------------------------------------------------

def resolve_minutes(input_str: str) -> dict:
    """Parse minutes URL or bare token. Returns {token, host}."""
    if '/' not in input_str:
        return {'token': input_str, 'host': DOC_HOST}
    parsed = urlparse(input_str)
    host = parsed.hostname or DOC_HOST
    m = re.search(r'/minutes/([A-Za-z0-9]+)', parsed.path)
    token = m.group(1) if m else input_str
    return {'token': token, 'host': host}


# ---------------------------------------------------------------------------
# Low-level REST fetchers
# ---------------------------------------------------------------------------

def _check(resp: dict) -> dict:
    """Validate a Minutes REST response and return its `data` field."""
    if resp.get('status') != 200:
        raise RuntimeError(f"Minutes API HTTP {resp.get('status')}")
    body = resp.get('data') or {}
    if isinstance(body, str):
        raise RuntimeError(f"Minutes API non-JSON response: {body[:200]}")
    if body.get('code') not in (0, None):
        raise RuntimeError(f"Minutes API error code={body.get('code')} msg={body.get('msg')}")
    return body.get('data') or {}


def fetch_status(cookies, host: str, token: str) -> dict:
    res = http_get(cookies, host, f'/minutes/api/status?object_token={token}&language=zh_cn')
    return _check(res)


def fetch_base_info(cookies, host: str, token: str) -> dict:
    """GET /minutes/api/base_info_v2 — only per-object endpoint that exposes
    the meeting `topic` (title) along with `start_time`, `owner_info`,
    `keywords`, `web_vtt_url` (subtitle file URL — useful for future media-
    download work), and `summary_types[]` (the canonical section_id → name
    mapping: 17=章节纪要 / 18=总结 / 19=待办).
    """
    url = f'/minutes/api/base_info_v2?object_token={token}&language=zh_cn'
    return _check(http_get(cookies, host, url))


# Pagination tunables. Kept here so unit tests can monkey-patch them down
# instead of mocking transcript-sized fixtures.
_PARAGRAPH_IDS_PAGE = 1000
_PARTICIPANTS_PAGE = 200
_SUBTITLES_PAGE = 500
_PAGINATION_SAFETY_CAP = 200  # max round-trips per fetcher; runaway guard


def fetch_paragraph_ids(cookies, host: str, token: str) -> list:
    """Pull all paragraph IDs. Server paginates by page_num/page_size, but we
    used to ask for 10000 in one go; if real count exceeds the server's hard
    cap (unknown), it would silently truncate. Loop until a short page or
    `has_more=false` proves we're done.
    """
    out = []
    page_num = 0
    while page_num < _PAGINATION_SAFETY_CAP:
        url = (f'/minutes/api/subtitles/paragraph-ids?page_size={_PARAGRAPH_IDS_PAGE}'
               f'&page_num={page_num}&object_token={token}&language=zh_cn')
        data = _check(http_get(cookies, host, url))
        batch = data.get('list') or []
        out.extend(batch)
        if len(batch) < _PARAGRAPH_IDS_PAGE:
            return out
        if data.get('has_more') is False:
            return out
        page_num += 1
    print(f'[minutes] paragraph_ids hit pagination safety cap '
          f'({_PAGINATION_SAFETY_CAP} pages, {len(out)} items)', file=sys.stderr)
    return out


def fetch_speakers(cookies, host: str, token: str) -> dict:
    """No documented pagination on this endpoint — we ask for 10k and rely on
    the server-reported `total` (= paragraph count) to detect truncation.
    """
    url = (f'/minutes/api/speakers?size=10000&translate_lang=default'
           f'&object_token={token}&language=zh_cn')
    data = _check(http_get(cookies, host, url))
    total = data.get('total')
    pmap_len = (len(data.get('paragraph_to_speaker') or {})
                + len(data.get('paragraph_to_device_owner') or {}))
    if isinstance(total, int) and total > 0 and pmap_len > 0 and pmap_len < total:
        print(f'[minutes] speakers may be truncated: server reports total={total} '
              f'paragraphs but only {pmap_len} have a speaker mapping', file=sys.stderr)
    return data


def fetch_participants(cookies, host: str, token: str) -> dict:
    """Paginate participants via offset until a short page or total exhausted."""
    first_resp = None
    all_items = []
    offset = 0
    rounds = 0
    while rounds < _PAGINATION_SAFETY_CAP:
        url = (f'/minutes/api/participants?offset={offset}&size={_PARTICIPANTS_PAGE}'
               f'&object_token={token}&language=zh_cn')
        data = _check(http_get(cookies, host, url))
        if first_resp is None:
            first_resp = dict(data)  # preserve top-level fields (total, etc.)
        batch = data.get('list') or []
        all_items.extend(batch)
        total = data.get('total')
        if len(batch) < _PARTICIPANTS_PAGE:
            break
        if isinstance(total, int) and len(all_items) >= total:
            break
        offset += _PARTICIPANTS_PAGE
        rounds += 1
    else:
        print(f'[minutes] participants hit pagination safety cap '
              f'({_PAGINATION_SAFETY_CAP} pages, {len(all_items)} items)', file=sys.stderr)
    if first_resp is None:
        return {'list': []}
    first_resp['list'] = all_items
    return first_resp


def fetch_summaries(cookies, host: str, token: str) -> dict:
    url = (f'/minutes/api/summaries_v2?translate_lang=default&ai_type=3'
           f'&object_token={token}&language=zh_cn')
    return _check(http_get(cookies, host, url))


# ---------------------------------------------------------------------------
# User-scoped fetchers (list / search) — no token required, walks user's
# minutes home page. Hosted on DOC_HOST by default; cross-tenant queries
# need a `host` override.
# ---------------------------------------------------------------------------

_LIST_PAGE = 20      # server-side default page size
_SEARCH_PAGE = 15    # server-side default page size


def fetch_space_list(cookies, host: str, max_items: int = 50,
                     owner_type: int = 1) -> list:
    """Paginate `GET /minutes/api/space/list` via timestamp cursor.

    First call has no `timestamp`. Each response carries `data.timestamp` that
    must be echoed as the next page's cursor. `has_more=false` terminates.
    `owner_type=1` matches the default "我的妙记 / 全部" tab.
    """
    items = []
    timestamp = None
    rounds = 0
    while len(items) < max_items and rounds < _PAGINATION_SAFETY_CAP:
        url = (f'/minutes/api/space/list?size={_LIST_PAGE}&space_name=1'
               f'&rank=1&asc=false&note_info=true&owner_type={owner_type}'
               f'&language=zh_cn')
        if timestamp is not None:
            url += f'&timestamp={timestamp}'
        data = _check(http_get(cookies, host, url))
        batch = data.get('list') or []
        if not batch:
            break
        items.extend(batch)
        if data.get('has_more') is False:
            break
        next_ts = data.get('timestamp')
        if not next_ts or next_ts == timestamp:
            break
        timestamp = next_ts
        rounds += 1
    if rounds >= _PAGINATION_SAFETY_CAP:
        print(f'[minutes] list hit pagination safety cap '
              f'({_PAGINATION_SAFETY_CAP} pages, {len(items)} items)',
              file=sys.stderr)
    return items[:max_items]


def fetch_search(cookies, host: str, query: str, max_items: int = 20) -> dict:
    """Paginate `GET /minutes/api/search` via offset cursor.

    Returns {meetings, total, query}. The server reports `realOffset` (next
    page's offset) and `has_more`; `suggestion_limit=0` skips autocomplete
    suggestions we don't render anyway.
    """
    if not query:
        return {'meetings': [], 'total': 0, 'query': ''}
    encoded = urlquote(query, safe='')
    items = []
    offset = 0
    total = None
    rounds = 0
    while len(items) < max_items and rounds < _PAGINATION_SAFETY_CAP:
        url = (f'/minutes/api/search?query={encoded}&suggestion_limit=0'
               f'&size={_SEARCH_PAGE}&offset={offset}&language=zh_cn')
        data = _check(http_get(cookies, host, url))
        meetings = data.get('meetings') or []
        if not meetings:
            break
        items.extend(meetings)
        if total is None:
            total = (data.get('total') or {}).get('value')
        if data.get('has_more') is False:
            break
        next_offset = data.get('realOffset')
        if next_offset is None or next_offset <= offset:
            break
        offset = next_offset
        rounds += 1
    if rounds >= _PAGINATION_SAFETY_CAP:
        print(f'[minutes] search hit pagination safety cap '
              f'({_PAGINATION_SAFETY_CAP} pages, {len(items)} items)',
              file=sys.stderr)
    return {'meetings': items[:max_items], 'total': total, 'query': query}


def fetch_subtitles(cookies, host: str, token: str, first_pid: str, size: int) -> list:
    """Pull up to `size` paragraphs starting at `first_pid`, continuing via
    cursor pid if the server returns a short page.

    Protocol: `paragraph_id=<cursor>&size=N&forward=1` returns N paragraphs
    starting at <cursor> (inclusive). To continue, re-call with the last
    paragraph's pid as the new cursor — the first item of the next batch
    overlaps with the previous batch's last item; we dedupe via seen-pid set.
    """
    if not first_pid or size <= 0:
        return []
    out = []
    seen = set()
    cursor = first_pid
    rounds = 0
    while len(out) < size and rounds < _PAGINATION_SAFETY_CAP:
        batch_size = min(_SUBTITLES_PAGE, size - len(out) + 1)  # +1 for cursor overlap
        url = (f'/minutes/api/subtitles_v2?paragraph_id={cursor}&size={batch_size}'
               f'&forward=1&translate_lang=default&is_fluent=false&filter_speaker=true'
               f'&object_token={token}&language=zh_cn')
        batch = _check(http_get(cookies, host, url)).get('paragraphs') or []
        if not batch:
            break
        added = 0
        for p in batch:
            pid = paragraph_pid(p)
            if pid and pid in seen:
                continue
            if pid:
                seen.add(pid)
            out.append(p)
            added += 1
        last_pid = paragraph_pid(batch[-1])
        if added == 0 or not last_pid or last_pid == cursor:
            break
        cursor = last_pid
        rounds += 1
    if rounds >= _PAGINATION_SAFETY_CAP:
        print(f'[minutes] subtitles hit pagination safety cap '
              f'({_PAGINATION_SAFETY_CAP} rounds, {len(out)} paragraphs)', file=sys.stderr)
    if len(out) < size:
        # Less than what paragraph-ids said exists — partial transcript.
        print(f'[minutes] subtitles: requested {size} paragraphs, got {len(out)} '
              f'— transcript may be partial', file=sys.stderr)
    return out


# Markers used to distinguish "logged-out, served a login wall" from "real
# minutes page". Feishu's web tier serves the login page either via 302
# (most common) or — in some tenant configs — 200 + a login-form HTML body.
_LOGIN_PATH_MARKERS = (
    '/passport/web/login',
    '/suite/passport/login',
    'passport.feishu.cn/suite/passport',
)
_LOGIN_BODY_MARKERS = (
    '<title>登录',
    '<title>Log in',
    '"isLogin":false',
)


def fetch_nav_html(cookies, host: str, token: str) -> str:
    """GET the minutes page HTML; SSR-inlined summaries are extracted from it.

    Raises on auth/redirect failures so the caller doesn't mistake a logged-
    out session for "this minutes has no chapter summary". Three failure
    modes are detected:

      1. HTTP non-200 (302 to /passport/web/login, 401, 403, 404 …)
      2. HTTP 200 but body is a login page (rare; some tenants)
      3. HTTP 200 with a body that's empty or far too small to be a real
         SPA shell — usually means a gateway / proxy stripped the response

    A successfully-fetched but content-less minutes (genuine "no AI summary
    yet") still returns the HTML; the SSR parser handles emptiness downstream.
    """
    res = http_get(cookies, host, f'/minutes/{token}')
    status = res.get('status')
    headers = res.get('headers') or {}
    body = res.get('data')
    body_str = body if isinstance(body, str) else (json.dumps(body) if body else '')

    if status != 200:
        loc = headers.get('Location') or headers.get('location') or ''
        if 300 <= (status or 0) < 400 and any(m in loc for m in _LOGIN_PATH_MARKERS):
            raise RuntimeError(
                f'Minutes nav redirected to login ({status} → {loc}); '
                f'session likely expired. Run `lark login`.'
            )
        if status in (401, 403):
            raise RuntimeError(
                f'Minutes nav fetch denied (HTTP {status}); '
                f'session expired or no permission on /minutes/{token}.'
            )
        raise RuntimeError(
            f'Minutes nav fetch failed: HTTP {status} '
            f'(Location={loc or "?"})'
        )

    if body_str and any(m in body_str for m in _LOGIN_BODY_MARKERS):
        raise RuntimeError(
            'Minutes nav returned a login page (200 + login HTML); '
            'session expired. Run `lark login`.'
        )

    return body_str


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_time(ms) -> str:
    """Milliseconds → 'MM:SS' or 'HH:MM:SS' if ≥ 1h."""
    try:
        ms = int(ms)
    except Exception:
        return '00:00'
    s = ms // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f'{h:02d}:{m:02d}:{sec:02d}' if h else f'{m:02d}:{sec:02d}'


def fmt_unix_ms(ms) -> str:
    """ms since epoch → '2026-05-19 14:22' local time, '' if invalid."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return ''


def strip_em(s: str) -> str:
    """Search highlight uses `<em>...</em>`; render as markdown bold for terminal."""
    if not s:
        return ''
    return s.replace('<em>', '**').replace('</em>', '**')


def make_download_filename(token: str, topic: str = None,
                           start_time_ms=None) -> str:
    """Build a human-meaningful filename from minutes metadata.

    Preferred format: ``YYYY-MM-DD_<topic>_<token-short-8>.md``. We include
    the short token suffix unconditionally so two meetings with the same
    title on the same day still get unique filenames.

    Sanitization:
    - Replace filesystem-unsafe chars (``/\\:*?"<>|`` and ASCII control) with ``_``
    - Strip leading/trailing spaces, dots, and underscores
    - Truncate topic at 40 *characters* (works for CJK — we deliberately
      use len() not byte length, since macOS/Linux filesystems are byte-
      limited but byte-counting Chinese characters wastes the budget)

    Fallback: if neither topic nor start_time is usable, return the
    pre-existing format ``minutes_<full-token>.md`` so callers don't get
    a sudden filename-format change.
    """
    date_str = ''
    if start_time_ms:
        try:
            date_str = datetime.fromtimestamp(int(start_time_ms) / 1000).strftime('%Y-%m-%d')
        except Exception:
            pass
    topic_str = ''
    if topic:
        safe = re.sub(r'[/\\:*?"<>|\x00-\x1f]', '_', topic).strip(' ._')
        # Collapse consecutive underscores from the substitution
        safe = re.sub(r'_+', '_', safe)
        if len(safe) > 40:
            safe = safe[:40].rstrip(' ._')
        topic_str = safe
    if topic_str or date_str:
        parts = [p for p in (date_str, topic_str, token[:8]) if p]
        return '_'.join(parts) + '.md'
    # Total fallback — keeps the legacy filename shape.
    safe_token = re.sub(r'[/\\:*?"<>|]', '_', token)
    return f'minutes_{safe_token}.md'


def parse_text_cover(raw) -> str:
    """`text_cover` is a JSON-encoded string `{name, content, cover_info, ...}`.
    Return the `content` (one-line TLDR) or '' if not parseable."""
    if not raw or not isinstance(raw, str):
        return ''
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj.get('content') or ''
    except Exception:
        pass
    return ''


def paragraph_pid(paragraph: dict) -> str:
    sents = paragraph.get('sentences') or []
    return (sents[0].get('sid') if sents else None) or ''


def paragraph_start_ms(paragraph: dict) -> int:
    sents = paragraph.get('sentences') or []
    if not sents:
        return 0
    try:
        return int(sents[0].get('start_time') or 0)
    except Exception:
        return 0


def paragraph_text(paragraph: dict) -> str:
    parts = []
    for s in paragraph.get('sentences') or []:
        for c in s.get('contents') or []:
            parts.append(c.get('content') or '')
    return ''.join(parts)


def resolve_speaker(speakers_data: dict, pid: str) -> dict:
    """Return {name, key} for a paragraph's speaker (falls back to device owner)."""
    pmap = speakers_data.get('paragraph_to_speaker') or {}
    imap = speakers_data.get('speaker_info_map') or {}
    key = pmap.get(pid)
    if key and imap.get(key):
        return {'name': imap[key].get('user_name') or f'(speaker {key})', 'key': key}
    dmap = speakers_data.get('paragraph_to_device_owner') or {}
    dinfo = speakers_data.get('device_owner_map') or {}
    dkey = dmap.get(pid)
    if dkey and dinfo.get(dkey):
        return {'name': f'[设备 {dkey}]', 'key': dkey}
    return {'name': '(unknown)', 'key': None}


# ---------------------------------------------------------------------------
# SSR HTML summary extraction
# ---------------------------------------------------------------------------

# section_id observed:
#   17 = 章节纪要 (chapters, with title)
#   18 = 会议纪要 (one Markdown blob, no title)
#   19 = 关键讨论 / 待办 (each is a short fact, no title)
#
# Server-side Go map serialization does NOT preserve field order, so a single
# in-order regex won't work. We anchor on `"content_id":"<id>"` (always present
# and unique), scope to the slice up to the next content_id, and extract each
# field independently.

_CONTENT_ID_RE = re.compile(r'"content_id":"(\d+)"')
_FIELD_RES = {
    'section_id': re.compile(r'"section_id":(\d+)'),
    'start_time': re.compile(r'"start_time":(\d+)'),
    'stop_time':  re.compile(r'"stop_time":(\d+)'),
    'title':      re.compile(r'"title":"((?:[^"\\]|\\.)*)"'),
    'data':       re.compile(r'"data":"((?:[^"\\]|\\.)*)"'),
}


def _unesc_json_str(s: str) -> str:
    """Unescape a JSON-string body (after outer `"..."` quotes are stripped).

    Feishu SSR strings are double-stringified: a newline appears as the 3
    literal chars ``\\\\n`` (one extra backslash from the second pass). We
    feed the value through ``json.loads`` twice — first pass strips the outer
    backslash escapes, second pass turns the surviving ``\\n`` into a real
    newline. Falls back gracefully if the body isn't valid JSON.
    """
    if not s:
        return ''
    try:
        once = json.loads('"' + s + '"')
    except Exception:
        return s
    if not isinstance(once, str):
        return s
    try:
        return json.loads('"' + once + '"')
    except Exception:
        return once


def parse_ssr_summaries(html: str) -> dict:
    """Extract {meeting, chapters, key_discussions} from minutes nav HTML.

    Summary items are inline in a <script> tag with `"` encoded as the
    6 literal chars `\\u0022` and `\\n` for newlines. We first unescape
    `\\u0022` -> `"` so the rest looks like real JSON, then locate each
    item by anchoring on `"content_id":"<id>"` and extracting each field
    independently (field order varies because of Go map serialization).
    """
    text = html.replace(r'\u0022', '"')
    out = {'meeting': '', 'chapters': [], 'key_discussions': []}

    matches = list(_CONTENT_ID_RE.finditer(text))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        scope = text[start:end]

        def grab(key: str) -> str:
            mm = _FIELD_RES[key].search(scope)
            return mm.group(1) if mm else ''

        sid_raw = grab('section_id')
        if not sid_raw:
            continue
        item = {
            'content_id': m.group(1),
            'section_id': int(sid_raw),
            'start_time_ms': int(grab('start_time') or 0),
            'stop_time_ms': int(grab('stop_time') or 0),
            'title': _unesc_json_str(grab('title')),
            'data': _unesc_json_str(grab('data')),
        }
        if item['section_id'] == 18 and not out['meeting']:
            out['meeting'] = item['data']
        elif item['section_id'] == 17:
            out['chapters'].append(item)
        elif item['section_id'] == 19:
            out['key_discussions'].append(item)

    out['chapters'].sort(key=lambda x: x['start_time_ms'])
    out['key_discussions'].sort(key=lambda x: x['start_time_ms'])
    return out


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _render_participants_md(parts: dict) -> list:
    md = ['## 参会人\n']
    for p in (parts.get('list') or []):
        tag = '主持' if p.get('is_host_user') else ('外部' if p.get('is_external') else '')
        suffix = f' — {tag}' if tag else ''
        md.append(f'- {p.get("user_name")} ({p.get("department_name") or "?"}){suffix}')
    md.append('')
    return md


def _render_ssr_md(ssr: dict) -> list:
    md = []
    if ssr['meeting']:
        md.append('## 会议纪要\n')
        md.append(ssr['meeting'])
        md.append('')
    if ssr['chapters']:
        md.append('## 章节纪要\n')
        for c in ssr['chapters']:
            t = fmt_time(c['start_time_ms'])
            title = c['title'] or '(无标题)'
            md.append(f'### `{t}` {title}\n')
            md.append(c['data'])
            md.append('')
    if ssr['key_discussions']:
        md.append('## 关键讨论 / 待办\n')
        for k in ssr['key_discussions']:
            t = fmt_time(k['start_time_ms'])
            md.append(f'- `{t}` {k["data"]}')
        md.append('')
    return md


def _render_speaker_summary_md(summ: dict, speakers: dict) -> list:
    ai_speaker = (summ.get('ai_speaker_summary') or {}).get('details') or {}
    if not ai_speaker:
        return []
    imap = speakers.get('speaker_info_map') or {}
    md = ['## 智能记要（按说话人）\n']
    for key, item in ai_speaker.items():
        name = (imap.get(key) or {}).get('user_name') or f'speaker {key}'
        edited = ' *(已编辑)*' if item.get('edited') else ''
        md.append(f'### {name}{edited}\n')
        md.append(item.get('content') or '(无内容)')
        md.append('')
    return md


def _render_transcript_md(paragraphs: list, speakers: dict) -> list:
    md = ['## 文字记录\n']
    last_key = None
    for p in paragraphs:
        text = paragraph_text(p)
        if not text.strip():
            continue
        sp = resolve_speaker(speakers, paragraph_pid(p))
        time = fmt_time(paragraph_start_ms(p))
        if sp['key'] != last_key:
            md.append('')
            md.append(f'### {sp["name"]}')
            last_key = sp['key']
        md.append(f'`{time}` {text}')
    md.append('')
    return md


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_minutes_meta(cookies, input_str: str):
    info = resolve_minutes(input_str)
    status = fetch_status(cookies, info['host'], info['token'])
    parts = fetch_participants(cookies, info['host'], info['token'])
    speakers = fetch_speakers(cookies, info['host'], info['token'])
    video = status.get('video_info') or {}
    out = {
        'object_token': info['token'],
        'host': info['host'],
        'object_status': status.get('object_status'),
        'summary_status': status.get('summary_status'),
        'template_summary_status': status.get('template_summary_status'),
        'can_modify': status.get('can_modify'),
        'can_comment': status.get('can_comment'),
        'can_create_clip': status.get('can_create_clip'),
        'video': {
            'vid': video.get('vid'),
            'video_url': video.get('video_url'),
            'audio_url': video.get('audio_url'),
            'cover': video.get('video_cover'),
        },
        'participants': [
            {
                'user_id': p.get('user_id'),
                'user_name': p.get('user_name'),
                'department': p.get('department_name'),
                'is_host': p.get('is_host_user'),
                'is_external': p.get('is_external'),
                'is_paragraph_speaker': p.get('is_paragraph_speaker'),
            }
            for p in (parts.get('list') or [])
        ],
        'speakers': [
            {
                'key': k,
                'user_name': v.get('user_name'),
                'is_login_user': v.get('is_login_user'),
                'identify_method': v.get('identify_method'),
            }
            for k, v in (speakers.get('speaker_info_map') or {}).items()
        ],
        'paragraph_count': speakers.get('total'),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_minutes_transcript(cookies, input_str: str):
    info = resolve_minutes(input_str)
    pids = fetch_paragraph_ids(cookies, info['host'], info['token'])
    if not pids:
        print('(no transcript)', file=sys.stderr)
        return
    speakers = fetch_speakers(cookies, info['host'], info['token'])
    paragraphs = fetch_subtitles(cookies, info['host'], info['token'],
                                  first_pid=pids[0]['pid'], size=len(pids))
    md = _render_transcript_md(paragraphs, speakers)
    # Drop the leading "## 文字记录" header for the standalone command — caller
    # got what they asked for.
    if md and md[0].startswith('## '):
        md = md[1:]
    print('\n'.join(md).strip() + '\n')


def cmd_minutes_summary(cookies, input_str: str):
    info = resolve_minutes(input_str)
    summ = fetch_summaries(cookies, info['host'], info['token'])
    speakers = fetch_speakers(cookies, info['host'], info['token'])
    md = _render_speaker_summary_md(summ, speakers)
    if not md:
        print('(no AI summary — generation may be in progress)', file=sys.stderr)
        return
    if md[0].startswith('## '):
        md = md[1:]  # drop section header for standalone
    md.insert(0, '# 智能记要（按说话人）\n')
    print('\n'.join(md).strip() + '\n')


def cmd_minutes_chapters(cookies, input_str: str):
    info = resolve_minutes(input_str)
    # fetch_nav_html raises on login-expired / non-200 — let it propagate so
    # the user sees the actionable RuntimeError instead of a misleading
    # "no chapter / meeting summary" hint.
    html = fetch_nav_html(cookies, info['host'], info['token'])
    ssr = parse_ssr_summaries(html)
    if not any([ssr['meeting'], ssr['chapters'], ssr['key_discussions']]):
        print('(no chapter / meeting summary in this minutes)', file=sys.stderr)
        return
    md = _render_ssr_md(ssr)
    print('\n'.join(md).strip() + '\n')


def cmd_minutes_download(cookies, input_str: str, output_path: str = None):
    info = resolve_minutes(input_str)
    # Best-effort metadata — used for the markdown H1 and (when --out is not
    # given) the filename. Failure here doesn't break the download since the
    # actual content fetches own their own auth-error reporting.
    base_info = {}
    try:
        base_info = fetch_base_info(cookies, info['host'], info['token'])
    except Exception:
        pass
    topic = base_info.get('topic') or ''
    start_time = base_info.get('start_time') or 0

    # Fetch everything
    pids = fetch_paragraph_ids(cookies, info['host'], info['token'])
    speakers = fetch_speakers(cookies, info['host'], info['token'])
    parts = fetch_participants(cookies, info['host'], info['token'])
    summ = fetch_summaries(cookies, info['host'], info['token'])
    paragraphs = (fetch_subtitles(cookies, info['host'], info['token'],
                                    first_pid=pids[0]['pid'], size=len(pids))
                  if pids else [])
    # fetch_nav_html now raises on auth failure; empty `html` here means a
    # genuine 200 with no body — parse_ssr_summaries handles that gracefully.
    html = fetch_nav_html(cookies, info['host'], info['token'])
    ssr = parse_ssr_summaries(html)

    date_str = fmt_unix_ms(start_time)[:10] if start_time else ''
    header = f'# 妙记: {topic or info["token"]}'
    if date_str:
        header += f' ({date_str})'
    md = [header + '\n']
    md += _render_participants_md(parts)
    md += _render_ssr_md(ssr)
    md += _render_speaker_summary_md(summ, speakers)
    if paragraphs:
        md += _render_transcript_md(paragraphs, speakers)

    content = '\n'.join(md).strip() + '\n'
    from ..paths import resolve_output_path
    default_name = make_download_filename(info['token'], topic=topic,
                                          start_time_ms=start_time)
    output_path = resolve_output_path(output_path, default_name)
    abs_path = os.path.abspath(output_path)
    with open(abs_path, 'w', encoding='utf-8') as f:
        f.write(content)

    ai_speaker = (summ.get('ai_speaker_summary') or {}).get('details') or {}
    print(json.dumps({
        'saved': abs_path,
        'size_bytes': len(content.encode('utf-8')),
        'topic': topic or None,
        'date': date_str or None,
        'sections': {
            'participants': len(parts.get('list') or []),
            'meeting_summary': bool(ssr['meeting']),
            'chapters': len(ssr['chapters']),
            'key_discussions': len(ssr['key_discussions']),
            'speaker_summaries': len(ai_speaker),
            'transcript_paragraphs': len(paragraphs),
        },
    }, ensure_ascii=False, indent=2))


def _list_entry_compact(x: dict) -> dict:
    """Slim down a /space/list raw entry to fields useful in JSON output."""
    return {
        'object_token': x.get('object_token'),
        'topic': x.get('topic'),
        'owner_name': x.get('owner_name'),
        'duration_ms': x.get('duration'),
        'create_time': x.get('create_time'),
        'time': x.get('time'),
        'url': x.get('url'),
        'note_url': x.get('note_url') or None,
        'note_title': x.get('note_title') or None,
        'note_status': x.get('note_status'),
        'media_type': x.get('media_type'),
        'meeting_id': x.get('meeting_id') or None,
        'is_owner': x.get('is_owner'),
        'tldr': parse_text_cover(x.get('text_cover')),
    }


def cmd_minutes_list(cookies, limit: int = 50, as_json: bool = False,
                     host: str = None):
    host = host or DOC_HOST
    items = fetch_space_list(cookies, host, max_items=limit)
    if as_json:
        print(json.dumps([_list_entry_compact(x) for x in items],
                         ensure_ascii=False, indent=2))
        return
    if not items:
        print('(no minutes found)', file=sys.stderr)
        return
    md = [f'# 妙记列表 ({len(items)} 条)\n']
    for x in items:
        topic = x.get('topic') or '(no title)'
        owner = x.get('owner_name') or '?'
        dur = fmt_time(x.get('duration') or 0)
        time_str = fmt_unix_ms(x.get('create_time'))
        token = x.get('object_token') or ''
        url = x.get('url') or ''
        tldr = parse_text_cover(x.get('text_cover'))
        note_url = x.get('note_url') or ''
        md.append(f'## `{time_str}` {topic} · `{dur}` · {owner}')
        md.append(f'- token: `{token}`')
        md.append(f'- url: {url}')
        if note_url:
            md.append(f'- 智能纪要 docx: {note_url}')
        if tldr:
            snippet = tldr[:200] + ('…' if len(tldr) > 200 else '')
            md.append(f'- TLDR: {snippet}')
        md.append('')
    print('\n'.join(md).strip() + '\n')


def cmd_minutes_search(cookies, query: str, limit: int = 20,
                       as_json: bool = False, host: str = None):
    host = host or DOC_HOST
    result = fetch_search(cookies, host, query, max_items=limit)
    meetings = result.get('meetings') or []
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if not meetings:
        print(f'(no minutes match "{query}")', file=sys.stderr)
        return
    total = result.get('total')
    total_str = f'{len(meetings)}/{total}' if total is not None else str(len(meetings))
    md = [f'# 搜索: "{query}" ({total_str} 条)\n']
    for x in meetings:
        topic = strip_em(x.get('topic') or '(no title)')
        dur = fmt_time(x.get('duration') or 0)
        token = x.get('object_token') or ''
        url = x.get('url') or ''
        md.append(f'## {topic} · `{dur}`')
        md.append(f'- token: `{token}`')
        if url:
            md.append(f'- url: {url}')
        hl = x.get('highlight') or {}
        summaries = hl.get('summaries') or []
        sentences = hl.get('sentences') or []
        if summaries:
            md.append('  - **章节命中**:')
            for s in summaries[:3]:
                md.append(f'    > {strip_em(s.get("content") or "")}')
        if sentences:
            md.append('  - **转写命中**:')
            for s in sentences[:5]:
                md.append(f'    > {strip_em(s.get("text") or "")}')
        md.append('')
    print('\n'.join(md).strip() + '\n')
