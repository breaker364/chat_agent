"""
audit.py — Local audit + debug logging for write/read operations.

Two log files:
  ~/.lark_cli/audit.log   write ops only; always on; never rotated (forensic)
  ~/.lark_cli/debug.log   reads + writes; opt-in via LARK_DEBUG_LOG=1; 10MB rollover

Two context managers:
  audit_write(cmd, target, extra)  always records to audit.log;
                                   also debug.log if LARK_DEBUG_LOG=1
  audit_read(cmd, target, extra)   no-op unless LARK_DEBUG_LOG=1;
                                   records to debug.log only

Best-effort: filesystem failures emit `[audit]` warnings to stderr but never
break the wrapped command. Exceptions inside the with block flip ok=False
and are re-raised transparently.
"""

import json
import logging
import logging.handlers
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DATA_DIR

AUDIT_LOG_PATH = os.path.join(DATA_DIR, 'audit.log')
DEBUG_LOG_PATH = os.path.join(DATA_DIR, 'debug.log')
ERROR_LOG_PATH = os.path.join(DATA_DIR, 'error.log')
BLOCK_OPS_PATH = os.path.join(DATA_DIR, 'block_ops')

_DEBUG_ENABLED = os.environ.get('LARK_DEBUG_LOG') == '1'

_ROTATE_MAX_BYTES = 10 * 1024 * 1024
_ROTATE_BACKUPS = 1

# Operations blocked by default. User can override entirely by creating
# ~/.lark_cli/block_ops (one op per line); the file content REPLACES this set.
_DEFAULT_BLOCKED_OPS = frozenset({'send.text', 'send.media', 'send.recall'})

_audit_logger = None
_debug_logger = None
_error_logger = None


class _FsyncFileHandler(logging.FileHandler):
    """FileHandler that fsyncs after every emit for crash-safety."""

    def emit(self, record):
        super().emit(record)
        try:
            self.flush()
            if self.stream:
                os.fsync(self.stream.fileno())
        except Exception:
            pass


class _FsyncRotatingHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler + fsync after every emit."""

    def emit(self, record):
        super().emit(record)
        try:
            self.flush()
            if self.stream:
                os.fsync(self.stream.fileno())
        except Exception:
            pass


def _maybe_chmod(path: str) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows or unprivileged


def _make_logger(name: str, handler: logging.Handler) -> logging.Logger:
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger = logging.getLogger(name)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _ensure_audit_logger() -> logging.Logger:
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger
    os.makedirs(DATA_DIR, exist_ok=True)
    handler = _FsyncFileHandler(AUDIT_LOG_PATH)
    _audit_logger = _make_logger('lark_tools.audit', handler)
    _maybe_chmod(AUDIT_LOG_PATH)
    return _audit_logger


def _ensure_debug_logger() -> logging.Logger:
    global _debug_logger
    if _debug_logger is not None:
        return _debug_logger
    os.makedirs(DATA_DIR, exist_ok=True)
    handler = _FsyncRotatingHandler(
        DEBUG_LOG_PATH,
        maxBytes=_ROTATE_MAX_BYTES,
        backupCount=_ROTATE_BACKUPS,
    )
    _debug_logger = _make_logger('lark_tools.debug', handler)
    _maybe_chmod(DEBUG_LOG_PATH)
    return _debug_logger


def _ensure_error_logger() -> logging.Logger:
    global _error_logger
    if _error_logger is not None:
        return _error_logger
    os.makedirs(DATA_DIR, exist_ok=True)
    handler = _FsyncRotatingHandler(
        ERROR_LOG_PATH,
        maxBytes=_ROTATE_MAX_BYTES,
        backupCount=_ROTATE_BACKUPS,
    )
    _error_logger = _make_logger('lark_tools.error', handler)
    _maybe_chmod(ERROR_LOG_PATH)
    return _error_logger


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _load_user_id_safe():
    try:
        from .auth import load_user_id
        return load_user_id()
    except Exception:
        return None


def _resolve_user_name_safe(uid: str):
    """Return the operator's display name for the given uid.

    Two-tier lookup:
      1. Cached value from auth.load_user_name() — instant, no API call
      2. Lazy fetch via user._fetch_one() if cache miss; result is cached
         to keyring so subsequent audits hit tier 1.

    Any failure (no cookies, network error, keyring unavailable) returns
    None silently — the audit record just omits the user_name field.
    Audit must never break the wrapped command.
    """
    try:
        from .auth import load_user_name, save_user_name, load_cookies
        cached = load_user_name()
        if cached:
            return cached
        cookies = load_cookies()
        if not cookies:
            return None
        from .commands.user import _fetch_one
        profile = _fetch_one(cookies, uid)
        if profile and profile.get('name'):
            save_user_name(profile['name'])
            return profile['name']
    except Exception:
        pass
    return None


def _build_record(kind: str, cmd: str, target, ok: bool, latency_ms: int, extra) -> dict:
    rec = {
        'ts': _utc_now_iso(),
        'kind': kind,
        'cmd': cmd,
        'target': target,
        'ok': ok,
        'latency_ms': latency_ms,
    }
    uid = _load_user_id_safe()
    if uid:
        rec['user_id'] = uid
        name = _resolve_user_name_safe(uid)
        if name:
            rec['user_name'] = name
    if extra:
        rec['extra'] = extra
    return rec


def _emit(logger: logging.Logger, record: dict) -> None:
    try:
        logger.info(json.dumps(record, ensure_ascii=False))
    except Exception as e:
        print(f'[audit] write failed: {e}', file=sys.stderr)


_BLOCK_OPS_TEMPLATE = """# Lark CLI write-op blocklist.
# - Uncomment a line below to block that op; comment with `#` to allow.
# - Empty lines and `#` lines are ignored.
# - This file fully controls which write ops are blocked.
#
# Available write ops:
#   send.text  send.media  send.recall
#   doc.create  doc.append  doc.replace  doc.set-title  doc.delete-block
#   doc.edit  doc.edit-code  doc.insert-image
#   bitable.create  bitable.add-record  bitable.set-record
#   bitable.delete-record  bitable.add-field  bitable.rename-field

send.text
send.media
send.recall
"""


def _ensure_block_ops_file() -> None:
    """Create block_ops file with default content + helpful comments
    on first call. Subsequent calls are no-ops.
    """
    if os.path.exists(BLOCK_OPS_PATH):
        return
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(BLOCK_OPS_PATH, 'w', encoding='utf-8') as f:
            f.write(_BLOCK_OPS_TEMPLATE)
        try:
            os.chmod(BLOCK_OPS_PATH, 0o600)
        except OSError:
            pass
    except Exception:
        pass  # best-effort; fall back to in-memory default


def _load_blocked_ops() -> set:
    """Load the set of currently blocked write ops from block_ops file.
    Lazily creates the file with default content if missing so the user
    can edit it directly to add/remove blocks.
    """
    _ensure_block_ops_file()
    try:
        with open(BLOCK_OPS_PATH, 'r', encoding='utf-8') as f:
            return {
                line.strip()
                for line in f
                if line.strip() and not line.lstrip().startswith('#')
            }
    except Exception:
        return set(_DEFAULT_BLOCKED_OPS)


def _emit_block_error(cmd: str) -> None:
    print(f'[lark] BLOCKED: {cmd}', file=sys.stderr)
    print(f'[lark] to allow: comment out `{cmd}` in ~/.lark_cli/block_ops', file=sys.stderr)
    print(json.dumps({
        'error': 'OP_BLOCKED',
        'op': cmd,
        'config': '~/.lark_cli/block_ops',
        'how_to_allow': f'comment out `{cmd}` in ~/.lark_cli/block_ops',
    }, ensure_ascii=False))


@contextmanager
def audit_write(cmd: str, target=None, extra=None):
    """Wrap a WRITE op. Always records to audit.log.
    Also records to debug.log when LARK_DEBUG_LOG=1.
    Echoes a one-line summary to stderr before the op runs so AI agents
    and users can see what is about to be written.
    Refuses to run if the op is in the configured block list (default:
    `send.recall`); records the blocked attempt then exits non-zero.
    """
    if cmd in _load_blocked_ops():
        record = _build_record('write', cmd, target, ok=False, latency_ms=0, extra=extra)
        record['blocked'] = True
        try:
            _emit(_ensure_audit_logger(), record)
        except Exception:
            pass
        _emit_block_error(cmd)
        sys.exit(1)
    summary_target = f' → {target}' if target else ''
    summary_extra = f' {extra}' if extra else ''
    print(f'[lark] write: {cmd}{summary_target}{summary_extra}', file=sys.stderr)
    start = time.monotonic()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        latency_ms = int((time.monotonic() - start) * 1000)
        record = _build_record('write', cmd, target, ok, latency_ms, extra)
        try:
            _emit(_ensure_audit_logger(), record)
        except Exception as e:
            print(f'[audit] init failed: {e}', file=sys.stderr)
        if _DEBUG_ENABLED:
            try:
                _emit(_ensure_debug_logger(), record)
            except Exception as e:
                print(f'[audit] debug init failed: {e}', file=sys.stderr)


@contextmanager
def audit_read(cmd: str, target=None, extra=None):
    """Wrap a READ op. No-op unless LARK_DEBUG_LOG=1.
    When enabled, records to debug.log only (never audit.log).
    """
    if not _DEBUG_ENABLED:
        yield
        return
    start = time.monotonic()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        latency_ms = int((time.monotonic() - start) * 1000)
        record = _build_record('read', cmd, target, ok, latency_ms, extra)
        try:
            _emit(_ensure_debug_logger(), record)
        except Exception as e:
            print(f'[audit] debug init failed: {e}', file=sys.stderr)


# ---------------------------------------------------------------------------
# error.log — every CLI-level exception, with traceback. Always on, no opt-in.
# ---------------------------------------------------------------------------

# Flags whose VALUE may carry sensitive content (message body, file content,
# auth token, etc.). The flag name is preserved; the next argv token is
# replaced with '<redacted>' so error.log is safe to share.
_REDACT_FLAGS = frozenset({
    '--text', '--message', '--content', '--body',
    '--token', '--cookie', '--password',
})


def redact_argv(argv) -> list:
    """Return argv with values after sensitive flags replaced by <redacted>."""
    out = []
    skip_next = False
    for a in argv:
        if skip_next:
            out.append('<redacted>')
            skip_next = False
            continue
        out.append(a)
        if a in _REDACT_FLAGS:
            skip_next = True
    return out


def log_error(cmd: str, argv, exc: BaseException) -> None:
    """Append a structured error record to error.log. Best-effort.

    `cmd` should be the subcommand name (e.g. 'send', 'doc edit'); `argv`
    is the full argv list (will be redacted internally); `exc` is the
    exception caught at cli.main()'s top level.
    """
    import traceback
    record = {
        'ts': _utc_now_iso(),
        'kind': 'error',
        'cmd': cmd,
        'argv': redact_argv(list(argv)),
        'exception': type(exc).__name__,
        'message': str(exc),
        'traceback': traceback.format_exc(),
    }
    uid = _load_user_id_safe()
    if uid:
        record['user_id'] = uid
        name = _resolve_user_name_safe(uid)
        if name:
            record['user_name'] = name
    try:
        _emit(_ensure_error_logger(), record)
    except Exception as e:
        print(f'[audit] error log write failed: {e}', file=sys.stderr)
