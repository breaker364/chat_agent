"""
wiki_delete.py — internal-only helper to move a wiki node to trash.

Used by live-test fixtures to auto-clean DELETE_ME scratch files. NOT
exposed via the CLI on purpose: deletion is destructive, and the only
known caller is test teardown. If you ever want a user-facing `lark sheet
delete`, gate it behind heavy confirmation prompts.

Endpoint reverse-engineered 2026-05-18 (drive trash sniff):

  POST /space/api/wiki/v2/tree/del_node/
  body: {
    space_id:         <wiki space ID>,
    wiki_token:       <node to delete>,
    auto_delete_mode: 2,        # meaning unknown, copied from capture
    synergy_uuid:     <ms ts>,  # client uniqueness tag
    apply:            1,        # meaning unknown, copied from capture
  }
  response: { code: 0, data: { task_id, sync_checked: true, ... } }

Sends the node to trash (NOT permanent delete). Feishu's recycle bin
purges automatically after a retention window. Live tests don't need
permanent delete — moving to trash unblocks the listing UI which is the
practical goal.

Works for any wiki node type — sheet / docx / bitable / slides / folder /
etc. The endpoint operates on the wiki tree, not on the underlying document
content, so all leaf types share the same request schema (verified
2026-05-18 by sniffing del_node calls for sheet + docx + bitable: bodies
were byte-identical except for the wiki_token / synergy_uuid).

Does NOT support documents that live outside a wiki space (e.g. personal
drive files without a wiki_token).
"""

from __future__ import annotations

import time

from .config import DOC_HOST
from .http_utils import http_post_with_cookies


def delete_wiki_node(cookies, wiki_token: str, space_id: str) -> dict:
    """Move a wiki node to trash. Returns the server-side `data` dict.

    Raises RuntimeError on non-zero code or unexpected response shape.
    """
    if not wiki_token:
        raise ValueError('wiki_token is required')
    if not space_id:
        raise ValueError('space_id is required')

    body = {
        'space_id': str(space_id),
        'wiki_token': wiki_token,
        'auto_delete_mode': 2,
        'synergy_uuid': str(int(time.time() * 1000)),
        'apply': 1,
    }
    res = http_post_with_cookies(cookies, DOC_HOST, '/space/api/wiki/v2/tree/del_node/', body)
    payload = res.get('data') if isinstance(res, dict) else None
    if not isinstance(payload, dict):
        raise RuntimeError(f'del_node failed: unexpected response body={payload!r}')
    if payload.get('code') != 0:
        raise RuntimeError(
            f'del_node failed: code={payload.get("code")} '
            f'msg={payload.get("msg") or payload.get("message")} body={payload}'
        )
    return payload.get('data') or {}
