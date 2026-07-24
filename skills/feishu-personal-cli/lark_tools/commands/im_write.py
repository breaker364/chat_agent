"""
commands/im_write.py - IM write commands (send message, recall).

Protocols:
- cmd 5 PUT_MESSAGE: send text / image / file / @mention
- cmd 9 DELETE_MESSAGE: recall a message by messageId
"""

import json
import random
import string
import sys  # noqa: F401  -- needed for sys.exit in error paths

from ..proto import encode_message
from ..gateway import send_gateway_request, decode_response
from ..im_upload import upload_local_file
from .chat import get_chat_msg_count_hint


def _rand_client_msg_id(length: int = 10) -> str:
    """Client-generated idempotency key. 10-char mixed-case alphanumeric per capture."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(random.choices(alphabet, k=length))


# ---------------------------------------------------------------------------
# Message body builders (cmd 5 payload.f2 / MessageContent)
#
# entities.RichText wire structure (per references/api/cmd_005_put_message.md):
#   f14 = RichText {
#     f1: repeated string elementIds     -- render order of elements
#     f2: string innerText               -- preview text
#     f3: RichTextElements {
#       f1: map<string, RichTextElement> dictionary   -- keyed by elementId
#     }
#   }
#   RichTextElement { f1: type, f3: property }
#     type=1 TEXT  -> property = TextProperty { f1: text }
#     type=5 AT    -> property = MentionProperty { f1: userId|"all", f2: "@display", f3: flag }
# ---------------------------------------------------------------------------

def _text_element(text: str) -> dict:
    """TEXT RichTextElement: {type=1, textProperty={text=...}}."""
    return {1: 1, 3: {1: text}}


def _at_element(user_id: str, display_name: str, flag: int = 0) -> dict:
    """AT_MENTION RichTextElement: {type=5, mentionProperty={userId, @display, flag}}.
    For @all, pass user_id='all' and display_name='@所有人'."""
    return {1: 5, 3: {1: user_id, 2: display_name, 3: flag}}


def _map_entry(key: str, value: dict) -> dict:
    """Protobuf map<K,V> wire MapEntry: {f1: key, f2: value}."""
    return {1: key, 2: value}


def _build_text_body(text: str, at_segments=None) -> dict:
    """
    Build MessageContent (payload.f2) for a text message.

    Args:
      text: the user's text content. Becomes the first element and prefixes innerText.
      at_segments: optional list of (user_id, display_name). Each becomes a trailing
        AT_MENTION element. For @all: ('all', '@所有人').

    Element layout:
      id="1" -> TEXT(text)                      (always present)
      id="2"..."N+1" -> AT_MENTION(user_i)      (one per at segment)

    innerText (preview) concatenates: text + " @display1" + " @display2" ...
    """
    element_ids = []
    entries = []
    preview_parts = []
    next_id = 1

    if text:
        eid = str(next_id)
        next_id += 1
        element_ids.append(eid)
        entries.append(_map_entry(eid, _text_element(text)))
        preview_parts.append(text)

    if at_segments:
        for user_id, display_name in at_segments:
            eid = str(next_id)
            next_id += 1
            # @ self / @ all => flag=0, @ others => flag=1 (per doc observation, 3 samples)
            flag = 1 if user_id != 'all' else 0
            element_ids.append(eid)
            entries.append(_map_entry(eid, _at_element(user_id, display_name, flag)))
            preview_parts.append(display_name)

    inner_text = ' '.join(preview_parts)
    rich_text = {
        1: element_ids,          # elementIds (repeated string)
        2: inner_text,           # innerText
        3: {1: entries},         # RichTextElements.dictionary (map wire = repeated MapEntry)
    }
    return {1: b'', 14: rich_text}


# ---------------------------------------------------------------------------
# Send commands
# ---------------------------------------------------------------------------

def _check_response(resp: dict) -> tuple:
    """Extract (success, status, packet, payload) from gateway response."""
    result = decode_response(resp['buffer'])
    packet = result.get('packet')
    payload = result.get('payload')
    status = packet.get('status', 0) if packet else None
    success = packet is not None and (not status or str(status) == '0')
    return success, status, packet, payload


# ---------------------------------------------------------------------------
# Send-time safety gate: block messages to chats with no prior history.
# Group sends (≥3 members) are unaffected. Failure to determine chat
# state → fail-closed.
#
# Algorithm:
#   1. cmd=44 PULL_CHAT_CHATTERS → payload.f2 lists chat members
#        - single dict → 1 member; list of N → N members
#   2. members >= 3 → group → allow (3+ chats can't be accidental cold contact)
#   3. members <= 1 → degenerate group (only me) → allow
#   4. members == 2 → ambiguous (P2P or 2-person group); both should be blocked
#      if the chat has zero real messages. Use cmd=64 (PULL_CHATS_BY_IDS) to
#      cheaply count messages via the f15 sentinel:
#        - hint > 0  → at least one historical message → allow
#        - hint == 0 → UINT64_MAX sentinel = empty chat → BLOCK
#        - hint None → cmd=64 failure → fail-closed BLOCK
#
# Why not cmd=5030 `exists` flag (an earlier approach): the flag means
# "P2P record initialized server-side", not "we have spoken". It's true
# whenever you've ever opened the contact's card, shared a group, or are in
# the same org — so cold contacts in the same company would slip through.
# ---------------------------------------------------------------------------

def _check_send_allowed(cookies, chat_id: str):
    """Returns (allowed: bool, reason: str, message: str)."""
    # 1. Pull chat metadata via cmd=44
    try:
        resp = send_gateway_request(cookies, 44, encode_message({1: chat_id, 2: True, 5: 5}))
        result = decode_response(resp['buffer'])
        packet = result.get('packet')
        payload = result.get('payload')
        status = packet and packet.get('status', 0)
        if not packet or (status and str(status) != '0'):
            return (False, 'METADATA_FETCH_FAILED',
                    f'Cannot fetch chat metadata for {chat_id} (status={status}). Send blocked.')
        members_field = payload.get('f2') if isinstance(payload, dict) else None
        if isinstance(members_field, list):
            member_count = len([m for m in members_field if isinstance(m, dict)])
        elif isinstance(members_field, dict):
            member_count = 1
        else:
            member_count = 0
    except Exception as e:
        return (False, 'METADATA_FETCH_ERROR',
                f'Error fetching chat metadata for {chat_id}: {e}. Send blocked.')

    if member_count >= 3 or member_count <= 1:
        # >=3: real group; <=1: degenerate group with only me — neither is a cold P2P
        return (True, '', '')

    # 2 members — block iff chat has zero real messages
    hint = get_chat_msg_count_hint(cookies, chat_id)
    if hint is None:
        return (False, 'HISTORY_LOOKUP_FAILED',
                f'Error checking chat history for {chat_id}. Send blocked.')
    if hint == 0:
        return (False, 'NO_CHAT_HISTORY',
                f'Send blocked: chat {chat_id} has no message history. '
                f'Only chats with prior messages and groups of 3+ members are allowed via CLI.')
    return (True, '', '')


def _emit_block_and_exit(reason: str, message: str):
    print(json.dumps({'error': 'SEND_BLOCKED', 'reason': reason, 'message': message}))
    sys.exit(1)


def cmd_send_text(cookies, chat_id: str, text: str, at_segments=None, raw: bool = False):
    """Send a text message (optionally with @mentions) via cmd 5."""
    allowed, reason, msg = _check_send_allowed(cookies, chat_id)
    if not allowed:
        _emit_block_and_exit(reason, msg)
    client_msg_id = _rand_client_msg_id()
    req = {
        1: 4,                           # messageType=4 = text
        2: _build_text_body(text, at_segments),
        3: chat_id,
        4: b'', 5: b'',                 # empty bytes per spec (must emit)
        6: client_msg_id,
        7: 1, 9: 1, 12: 0, 15: 0,
        16: b'',                        # empty bytes per spec
        17: 0, 19: 0,
    }
    payload_bytes = encode_message(req)
    resp = send_gateway_request(cookies, 5, payload_bytes)
    success, status, _packet, payload = _check_response(resp)

    if raw:
        print(json.dumps({'success': success, 'status': status, 'clientMsgId': client_msg_id, 'payload': payload}))
        return

    message_id = None
    position = None
    timestamp = None
    if isinstance(payload, dict):
        f1 = payload.get('f1')
        if isinstance(f1, dict):
            message_id = f1.get('f1')
            timestamp = f1.get('f4')
        f2 = payload.get('f2')
        if isinstance(f2, dict):
            position = f2.get('f2')

    out = {
        'success': success,
        'chatId': chat_id,
        'clientMsgId': client_msg_id,
        'messageId': message_id,
        'position': position,
        'timestamp': timestamp,
    }
    if not success:
        out['status'] = status
        out['error'] = 'SEND_FAILED'
    print(json.dumps(out))


def cmd_send_media(cookies, chat_id: str, local_path: str, raw: bool = False):
    """Upload a local image/file and send it via cmd 5.
    Media type is auto-detected from the file's MIME (image/* → image, else file).

    Content (PutMessageRequest.f2) is a proper Content message. Per the
    2026-04-19 capture-decoded schema:
      - image: {10: cryptoToken}  — NOT imageKey(id=2); new IM reads cryptoToken.
      - file:  {6: fileKey}       — bare upload_id, server auto-fills filename/mime/size.
    """
    # Gate runs BEFORE upload to avoid wasted bandwidth on a blocked send.
    allowed, reason, msg = _check_send_allowed(cookies, chat_id)
    if not allowed:
        _emit_block_and_exit(reason, msg)
    info = upload_local_file(cookies, local_path)
    is_image = info['isImage']
    msg_type = 5 if is_image else 3
    client_msg_id = _rand_client_msg_id()

    # For images, use the server-returned cryptoToken from /upload/complete
    # response (f2). This is a freshly-minted key distinct from upload_id and
    # must be used verbatim — client-side transforms of upload_id don't work.
    # For files, the upload_id itself goes into Content.fileKey.
    if is_image:
        if not info.get('cryptoToken'):
            print(json.dumps({'error': 'NO_CRYPTO_TOKEN',
                              'message': 'upload/complete did not return cryptoToken'}))
            sys.exit(1)
        content = {10: info['cryptoToken']}   # Content.cryptoToken
    else:
        content = {6: info['uploadId']}        # Content.fileKey

    # Match sample 2/3 capture: include isNotified=1, version=1 and explicit
    # zero/empty default fields that the captured client emits.
    req = {
        1: msg_type,
        2: content,
        3: chat_id,
        4: '', 5: '',          # rootId, parentId empty
        6: client_msg_id,
        7: 1,                  # isNotified
        9: 1,                  # version
        12: 0, 15: 0,          # isReplyInThread, isShare
        16: b'',               # aiInfoContext empty
        17: 0,                 # syncToChat
    }
    payload_bytes = encode_message(req)
    resp = send_gateway_request(cookies, 5, payload_bytes)
    success, status, _packet, payload = _check_response(resp)

    if raw:
        print(json.dumps({
            'success': success, 'status': status,
            'clientMsgId': client_msg_id, 'upload': info, 'payload': payload,
        }))
        return

    message_id = None
    position = None
    timestamp = None
    if isinstance(payload, dict):
        f1 = payload.get('f1')
        if isinstance(f1, dict):
            message_id = f1.get('f1')
            timestamp = f1.get('f4')
        f2 = payload.get('f2')
        if isinstance(f2, dict):
            position = f2.get('f2')

    out = {
        'success': success,
        'chatId': chat_id,
        'clientMsgId': client_msg_id,
        'messageId': message_id,
        'position': position,
        'timestamp': timestamp,
        'kind': 'image' if is_image else 'file',
        'filename': info['filename'],
        'size': info['size'],
        'contentKey': info['cryptoToken'] if is_image else info['uploadId'],
    }
    if not success:
        out['status'] = status
        out['error'] = 'SEND_FAILED'
    print(json.dumps(out))


def cmd_recall(cookies, message_id: str, raw: bool = False):
    """Recall (delete) a message via cmd 9."""
    payload_bytes = encode_message({1: message_id})
    resp = send_gateway_request(cookies, 9, payload_bytes)
    success, status, _packet, payload = _check_response(resp)

    if raw:
        print(json.dumps({'success': success, 'status': status, 'payload': payload}))
        return

    ack = None
    if isinstance(payload, dict):
        ack = payload.get('f1')

    out = {
        'success': success and ack == 'ok',
        'messageId': message_id,
        'serverAck': ack,
    }
    if not success:
        out['status'] = status
        out['error'] = 'RECALL_FAILED'
    print(json.dumps(out))
