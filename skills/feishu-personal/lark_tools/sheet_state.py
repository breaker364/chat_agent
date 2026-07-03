"""
sheet_state.py — local persistence for spreadsheet base_rev across CLI calls.

Why: each `lark sheet write-*` invocation needs the current revision of the
sheet to send a valid OT commit. We cache the latest known rev per
spreadsheet token in ~/.lark_state.json and bump it after every successful
write.

Layout:
  ~/.lark_state.json
  {
    "sheet_revs": {
      "<spreadsheet_token>": <int_revision>,
      ...
    }
  }

If the cached rev gets stale (manual edit in browser, server rejects), the
caller is expected to `lark sheet state-clear --token <t>` and let the next
write start fresh. Recovery from REJECT_COMMIT is not implemented (no sample
captured in reverse-engineering yet — see references/api/rest_post_space_api_v2_sheet_user_changes.md).
"""

import json
import os
from pathlib import Path

_STATE_PATH = Path.home() / '.lark_state.json'
_KEY = 'sheet_revs'


def _load() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(state: dict) -> None:
    _STATE_PATH.write_text(json.dumps(state, indent=2), encoding='utf-8')
    try:
        os.chmod(_STATE_PATH, 0o600)
    except OSError:
        pass


def get_rev(token: str, default: int = 0) -> int:
    return int(_load().get(_KEY, {}).get(token, default))


def set_rev(token: str, rev: int) -> None:
    state = _load()
    state.setdefault(_KEY, {})[token] = int(rev)
    _save(state)


def clear_rev(token: str) -> None:
    state = _load()
    if state.get(_KEY, {}).pop(token, None) is not None:
        _save(state)


def clear_all() -> None:
    state = _load()
    if _KEY in state:
        state.pop(_KEY)
        _save(state)


def list_revs() -> dict:
    return dict(_load().get(_KEY, {}))
