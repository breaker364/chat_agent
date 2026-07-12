"""
formatters.py - Extract meaningful fields from raw protobuf responses (mirrors lib/formatters.js)
"""

import re
from datetime import datetime, timezone, timedelta

SHANGHAI_TZ = timezone(timedelta(hours=8))


def strip_highlight(s):
    if not s:
        return ''
    if not isinstance(s, str):
        return ''
    s = re.sub(r'</?h>', '', s)
    s = re.sub(r'</?b>', '', s)
    s = re.sub(r'</?hb>', '', s)
    s = s.replace('&amp;', '&')
    s = re.sub(r'<[^>]*>', '', s)
    return s.strip()


def to_array(val):
    if val is None:
        return []
    return val if isinstance(val, list) else [val]


def get_locale_name(locales, lang='zh_cn'):
    if not locales:
        return ''
    arr = to_array(locales)
    for item in arr:
        if isinstance(item, dict) and item.get('f1') == lang:
            return item.get('f2', '')
    return ''


def parse_total(meta):
    if not meta or not meta.get('f5'):
        return None
    try:
        import json
        info = json.loads(meta['f5'])
        return info.get('total')
    except Exception:
        return None


def format_dept(dept):
    return {
        'deptId': dept.get('f1', ''),
        'name': dept.get('f2', ''),
        'nameZh': get_locale_name(dept.get('f10'), 'zh_cn'),
        'parentId': dept.get('f3') or None,
        'memberCount': int(dept['f12']) if dept.get('f12') else (int(dept['f5']) if dept.get('f5') else None),
    }


def format_search_contacts(payload):
    items = to_array(payload.get('f2') if payload else None)
    result = []
    for item in items:
        profile = item.get('f7', {}).get('f1') if isinstance(item, dict) else None
        result.append({
            'userId': item.get('f1', '') if isinstance(item, dict) else '',
            'name': strip_highlight(item.get('f3', '') if isinstance(item, dict) else ''),
            'department': item.get('f4', '') if isinstance(item, dict) else '',
            'email': (profile.get('f4') or profile.get('f9', '')) if isinstance(profile, dict) else '',
        })
    return result


def format_search_messages(payload):
    items = to_array(payload.get('f2') if payload else None)
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get('f7', {}).get('f5') if isinstance(item.get('f7'), dict) else None
        if not isinstance(meta, dict):
            meta = {}
        ts = int(meta['f3']) if meta.get('f3') else None
        preview = strip_highlight(item.get('f4', ''))
        colon_idx = preview.find(': ')
        result.append({
            'messageId': item.get('f1', ''),
            'chatName': strip_highlight(item.get('f3', '')),
            'sender': preview[:colon_idx] if colon_idx > 0 else '',
            'content': preview[colon_idx + 2:] if colon_idx > 0 else preview,
            'chatId': meta.get('f6', ''),
            'senderId': meta.get('f8', ''),
            'isP2P': meta.get('f14') == '1',
            'timestamp': ts,
            'time': _ts_to_locale(ts) if ts else None,
        })
    return result


def format_search_docs(payload):
    items = to_array(payload.get('f2') if payload else None)
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        meta11 = item.get('f7', {}).get('f11') if isinstance(item.get('f7'), dict) else None
        meta14 = item.get('f7', {}).get('f14') if isinstance(item.get('f7'), dict) else None
        url = owner = last_editor = wiki_space = ''
        if isinstance(meta11, dict):
            url = meta11.get('f4', '')
            owner = meta11.get('f5', '')
            last_editor = meta11.get('f13', '')
        elif isinstance(meta14, dict):
            url = meta14.get('f15', '')
            last_editor = meta14.get('f14', '')
            wiki_space = meta14.get('f18', '')
        result.append({
            'token': item.get('f1', ''),
            'title': strip_highlight(item.get('f3', '')),
            'url': url,
            'owner': owner,
            'lastEditor': last_editor,
            **(({'wikiSpace': wiki_space}) if wiki_space else {}),
        })
    return result


def format_search_groups(payload):
    items = to_array(payload.get('f2') if payload else None)
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        chat_meta = item.get('f7', {}).get('f3') if isinstance(item.get('f7'), dict) else None
        member_count = None
        if isinstance(chat_meta, dict) and chat_meta.get('f11'):
            try:
                member_count = int(chat_meta['f11'])
            except Exception:
                pass
        result.append({
            'chatId': item.get('f1', ''),
            'name': strip_highlight(item.get('f3', '')),
            'memberCount': member_count,
        })
    return result


def format_search_vc(payload):
    items = to_array(payload.get('f2') if payload else None)
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get('f2') != '16':
            continue
        meta = item.get('f7', {}).get('f13') if isinstance(item.get('f7'), dict) else None
        if not isinstance(meta, dict):
            meta = {}
        summary = item.get('f5', '')
        parts = summary.split(' | ')
        organizer = next((p.replace('组织者：', '').replace('组织者:', '') for p in parts if '组织者' in p), '')
        meeting_no = next((p.replace('ID: ', '').replace(' ', '') for p in parts if p.startswith('ID:')), '')
        time_str = parts[0] if parts else ''
        icon = meta.get('f5', '')
        is_1v1 = isinstance(icon, str) and '1v1_icon' in icon
        related_docs = item.get('f4')
        result.append({
            'meetingId': item.get('f1', ''),
            'title': strip_highlight(item.get('f3', '')),
            **(({'relatedDocs': strip_highlight(related_docs)}) if related_docs and related_docs != '<bytes:0>' else {}),
            'time': time_str,
            **(({'organizer': organizer}) if organizer else {}),
            **(({'meetingNo': meeting_no}) if meeting_no else {}),
            'type': '1v1' if is_1v1 else 'group',
            'url': meta.get('f2', ''),
        })
    return result


def format_dept_list(payload):
    groups = to_array(payload.get('f1') if payload else None)
    result = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        parent = format_dept(group['f2']) if group.get('f2') else None
        children = [format_dept(d) for d in to_array(group.get('f5')) if isinstance(d, dict)]
        result.append({'parent': parent, 'departments': children})
    return result


def format_dept_children(payload):
    items = to_array(payload.get('f1') if payload else None)
    return [format_dept(d) for d in items if isinstance(d, dict)]


def format_dept_members(payload):
    data = payload.get('f1') if payload else None
    if not isinstance(data, dict):
        return {'department': None, 'leader': None, 'members': []}
    leader_info = data.get('f1')
    dept_info = data.get('f2')
    leader = {
        'userId': leader_info.get('f1', ''),
        'name': leader_info.get('f2', ''),
        'email': leader_info.get('f25', ''),
    } if isinstance(leader_info, dict) else None
    department = format_dept(dept_info) if isinstance(dept_info, dict) else None
    members = [
        {'userId': m.get('f1', ''), 'name': m.get('f2', ''), 'email': m.get('f25', '')}
        for m in to_array(data.get('f3'))
        if isinstance(m, dict)
    ]
    return {'department': department, 'leader': leader, 'members': members}


def format_user_profile(profile):
    if not isinstance(profile, dict):
        return None
    return {
        'userId': profile.get('f1', ''),
        'name': profile.get('f2', ''),
        'email': profile.get('f25', ''),
        'bio': profile.get('f11', ''),
        'timezone': strip_highlight(profile.get('f30', '')) if profile.get('f30') else '',
    }


def strip_html(html):
    if not html or not isinstance(html, str):
        return ''
    html = re.sub(r'<at[^>]*>([^<]*)</at>', r'\1', html)
    html = re.sub(r'<img[^>]*>', '[图片]', html)
    html = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>[^<]*</a>', r'[\1]', html)
    html = re.sub(r'</?[^>]+>', '', html)
    html = html.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    return html.strip()


def format_chat_message(msg, opts=None):
    if not isinstance(msg, dict):
        return None
    opts = opts or {}
    ts = int(msg['f4']) if msg.get('f4') else None
    plain_text = msg.get('f5', {}).get('f1', '') if isinstance(msg.get('f5'), dict) else ''
    plain_text = plain_text if isinstance(plain_text, str) and not plain_text.startswith('<bytes:') else ''
    rich_content = msg.get('f5', {}).get('f2', '') if isinstance(msg.get('f5'), dict) else ''

    # Extract senderId: can be string (userId) or object {f1: {f4: "FROM_ID:xxx"}}
    sender_id = msg.get('f3', '')
    if isinstance(sender_id, dict):
        from_id = sender_id.get('f1', {}).get('f4', '') if isinstance(sender_id.get('f1'), dict) else ''
        sender_id = from_id[len('FROM_ID:'):] if isinstance(from_id, str) and from_id.startswith('FROM_ID:') else from_id or ''

    # Always strip HTML tags from content
    content = plain_text or (rich_content if isinstance(rich_content, str) else '')
    content = strip_html(content)

    result = {
        'messageId': msg.get('f1', ''),
        'senderId': sender_id,
        'timestamp': ts,
        'time': _ts_to_locale(ts) if ts else None,
        'content': content,
    }

    user_map = opts.get('userMap')
    if user_map:
        result['sender'] = user_map.get(sender_id, sender_id)

    if opts.get('verbose'):
        # `type` only matters for debugging / advanced use — keep gated.
        # messageId is always emitted (needed for downloads even in non-verbose).
        result['type'] = msg.get('f2', '')

    if opts.get('includeHtml') and isinstance(rich_content, str):
        result['html'] = rich_content

    # Image messages (type 5): imageKey in f5.f2.f1
    msg_type = msg.get('f2')
    if msg_type in ('5', 5):
        img_data = msg.get('f5', {}).get('f2') if isinstance(msg.get('f5'), dict) else None
        if isinstance(img_data, dict) and img_data.get('f1'):
            result['imageKey'] = img_data['f1']
            result['content'] = f"[图片:{img_data['f1']}]"

    # File messages (type 3): Content directly at f5 with
    # f1=fileKey, f2=filename, f3=size, f4=mimeType.
    # Schema from references/api/rest_get_download_messages_files.md.
    if msg_type in ('3', 3):
        file_data = msg.get('f5') if isinstance(msg.get('f5'), dict) else None
        if isinstance(file_data, dict) and file_data.get('f1'):
            result['fileKey'] = file_data['f1']
            filename = file_data.get('f2') or ''
            if filename:
                result['filename'] = filename
            size = file_data.get('f3')
            if size is not None:
                try:
                    result['size'] = int(size)
                except (ValueError, TypeError):
                    pass
            mime = file_data.get('f4')
            if mime:
                result['mime'] = mime
            label = filename or file_data['f1']
            result['content'] = f"[文件:{label}]"

    # Rich text messages (type 2): extract embedded imageKeys from f5.f6.f3.f1[].f2.f3.f17.f1
    if msg_type in ('2', 2):
        image_keys = []
        try:
            elements = msg.get('f5', {}).get('f6', {}).get('f3', {}).get('f1', [])
            if isinstance(elements, dict):
                elements = [elements]
            for el in elements:
                if not isinstance(el, dict):
                    continue
                f3 = el.get('f2', {}).get('f3', {})
                if not isinstance(f3, dict):
                    continue
                # imageKey can be at f17.f1 or directly at f2
                key = f3.get('f17', {}).get('f1', '') if isinstance(f3.get('f17'), dict) else ''
                if not key:
                    key = f3.get('f2', '')
                if isinstance(key, str) and key.startswith('img_'):
                    image_keys.append(key)
        except (AttributeError, TypeError):
            pass
        if image_keys:
            result['imageKeys'] = image_keys
            # Replace generic [图片] with [图片:key] for each embedded image
            text = result['content']
            for key in image_keys:
                text = text.replace('[图片]', f'[图片:{key}]', 1)
            result['content'] = text

    return result


def format_chat_messages(payload, opts=None):
    items = to_array(payload.get('f1') if payload else None)
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        msg = item.get('f2') or item
        formatted = format_chat_message(msg, opts)
        if formatted:
            result.append(formatted)
    return result


def format_message_detail(payload):
    if not payload or not payload.get('f1'):
        return None
    wrapper = payload['f1']
    if not isinstance(wrapper, dict):
        return None
    msg = wrapper.get('f2') or wrapper
    return format_chat_message(msg, {'includeHtml': True, 'verbose': True})


def _ts_to_locale(ts: int) -> str:
    """Convert Unix timestamp to Asia/Shanghai locale string."""
    dt = datetime.fromtimestamp(ts, tz=SHANGHAI_TZ)
    return dt.strftime('%Y/%m/%d %H:%M:%S')
