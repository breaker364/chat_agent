"""
auth.py - Authentication: cookie loading, auth check, QR login (mirrors lib/auth.js)

Session stored in the system keyring (macOS Keychain / Windows Credential Manager /
Linux Secret Service) via the `keyring` library. Requires a working keyring backend;
for headless Linux / Docker / CI, install a Secret Service provider (e.g.
gnome-keyring, KWallet, or a `keyring` third-party backend).
"""

import datetime
import time
import os
import sys
import subprocess

import keyring
import keyring.errors

from .config import DATA_DIR
from .proto import encode_message
from .gateway import send_gateway_request, decode_response
from .http_utils import http_post_json, sleep_ms

_KEYRING_SERVICE = 'lark_cli'
_SESSION_KEY = 'session'
_USERID_KEY = 'user_id'
_USERNAME_KEY = 'user_name'
_SESSION_ISSUED_KEY = 'session_issued_at'
_SESSION_LAST_CHECK_KEY = 'session_last_check'

# Local client-side session lifetime. Server cookie itself lives 365 days,
# but we expire it locally after this many days and force a re-login.
SESSION_MAX_AGE_DAYS = 7

_LEGACY_SESSION_FILE = os.path.join(DATA_DIR, 'session')
_LEGACY_USERID_FILE = os.path.join(DATA_DIR, 'user_id')

QR_LOGIN_HEADERS = {
    'x-api-version': '1.0.8',
    'x-app-id': '2',
    'x-device-info': 'device_id=0;device_name=lark-cli;device_os=Mac',
    'x-locale': 'zh-CN',
    'x-terminal-type': '2',
}

_SSH_ENV_VARS = ('SSH_CLIENT', 'SSH_CONNECTION', 'SSH_TTY')


# ==========================================
# Credential storage (system keyring)
# ==========================================

def _legacy_read(path: str):
    try:
        with open(path, 'r') as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def _keyring_help(e: Exception) -> str:
    """Platform-specific remediation hint for keyring failures."""
    err = str(e)
    if sys.platform.startswith('linux'):
        if 'Prompt dismissed' in err or 'Failed to create' in err:
            return (
                'Linux Secret Service prompt was dismissed or the default keyring is locked.\n'
                '  Desktop: rm ~/.local/share/keyrings/{login.keyring,default} && relogin; '
                'next `lark login` will recreate it (set empty password to auto-unlock).\n'
                '  SSH/WSL/headless: sudo apt install dbus-x11 gnome-keyring python3-secretstorage; '
                'then run `dbus-run-session -- lark <cmd>`.'
            )
        return (
            'sudo apt install gnome-keyring libsecret-1-0 python3-secretstorage. '
            'For SSH/WSL/headless wrap with `dbus-run-session -- lark <cmd>`.'
        )
    if sys.platform == 'darwin':
        return 'macOS Keychain access denied. Open Keychain Access and unlock the login keychain.'
    if sys.platform == 'win32':
        return 'Windows Credential Manager unavailable. Verify the service is running.'
    return 'Install a keyring backend (macOS Keychain / Windows Credential Manager / Linux Secret Service).'


def _read_secret(key: str, legacy_path: str):
    """Read from keyring. One-shot migrate from legacy plaintext file if present."""
    try:
        value = keyring.get_password(_KEYRING_SERVICE, key)
    except keyring.errors.KeyringError as e:
        raise RuntimeError(f'System keyring unavailable ({e}). {_keyring_help(e)}') from e

    if value:
        return value

    legacy = _legacy_read(legacy_path)
    if not legacy:
        return None
    keyring.set_password(_KEYRING_SERVICE, key, legacy)
    try:
        os.remove(legacy_path)
    except OSError:
        pass
    print(f'[auth] migrated {key} from {legacy_path} into system keyring', file=sys.stderr)
    return legacy


def _write_secret(key: str, legacy_path: str, value: str) -> None:
    try:
        keyring.set_password(_KEYRING_SERVICE, key, value)
    except keyring.errors.KeyringError as e:
        raise RuntimeError(f'System keyring unavailable ({e}). {_keyring_help(e)}') from e
    if os.path.exists(legacy_path):
        try:
            os.remove(legacy_path)
        except OSError:
            pass


def load_cookies():
    value = _read_secret(_SESSION_KEY, _LEGACY_SESSION_FILE)
    if not value:
        return None
    return [{'name': 'session', 'value': value, 'domain': '.feishu.cn'}]


def save_session(session_value: str) -> None:
    _write_secret(_SESSION_KEY, _LEGACY_SESSION_FILE, session_value)
    # Reset the local SESSION_MAX_AGE_DAYS clock on every fresh login.
    today = datetime.date.today().isoformat()
    try:
        keyring.set_password(_KEYRING_SERVICE, _SESSION_ISSUED_KEY, str(int(time.time())))
        keyring.set_password(_KEYRING_SERVICE, _SESSION_LAST_CHECK_KEY, today)
    except keyring.errors.KeyringError:
        pass


def load_user_id():
    return _read_secret(_USERID_KEY, _LEGACY_USERID_FILE)


def save_user_id(user_id: str) -> None:
    _write_secret(_USERID_KEY, _LEGACY_USERID_FILE, str(user_id))


def load_user_name():
    """Return the cached display name for the current user, or None.
    Cached lazily by audit.py the first time a write op runs."""
    try:
        return keyring.get_password(_KEYRING_SERVICE, _USERNAME_KEY)
    except keyring.errors.KeyringError:
        return None


def save_user_name(name: str) -> None:
    if not name:
        return
    try:
        keyring.set_password(_KEYRING_SERVICE, _USERNAME_KEY, str(name))
    except keyring.errors.KeyringError:
        pass


# ==========================================
# Get current user ID
# ==========================================

def get_current_user_id(cookies):
    cached = load_user_id()
    if cached:
        return cached

    # Fallback: extract from feed response
    payload = encode_message({1: 1, 2: 1, 3: 0, 4: 1, 5: 0, 7: 1, 10: 1, 11: 0})
    resp = send_gateway_request(cookies, 1000, payload)
    result = decode_response(resp['buffer'])
    resp_payload = result.get('payload')
    user_chat_str = resp_payload and resp_payload.get('f1', {}) and resp_payload['f1'].get('f2', {})
    if isinstance(user_chat_str, dict):
        user_chat_str = user_chat_str.get('f10')
    if user_chat_str and isinstance(user_chat_str, str) and ':' in user_chat_str:
        user_id = user_chat_str.split(':')[0]
        if user_id:
            save_user_id(user_id)
            print(f'[auth] userId {user_id} extracted from feed and cached', file=sys.stderr)
            return user_id
    return None


# ==========================================
# Auth health check
# ==========================================

def is_locally_expired() -> bool:
    """Local 7-day session age check, throttled to once per day.

    Returns True only on the first call of a new calendar day, when the session
    is older than SESSION_MAX_AGE_DAYS. Subsequent calls the same day skip the
    check (assume valid). Missing issued_at (legacy session) gets backfilled to
    now, so old users keep working but start a fresh 7-day clock.
    """
    today = datetime.date.today().isoformat()

    try:
        last_check = keyring.get_password(_KEYRING_SERVICE, _SESSION_LAST_CHECK_KEY)
    except keyring.errors.KeyringError:
        last_check = None
    if last_check == today:
        return False

    try:
        issued_str = keyring.get_password(_KEYRING_SERVICE, _SESSION_ISSUED_KEY)
    except keyring.errors.KeyringError:
        issued_str = None

    if not issued_str:
        try:
            keyring.set_password(_KEYRING_SERVICE, _SESSION_ISSUED_KEY, str(int(time.time())))
            keyring.set_password(_KEYRING_SERVICE, _SESSION_LAST_CHECK_KEY, today)
        except keyring.errors.KeyringError:
            pass
        return False

    try:
        issued_at = int(issued_str)
    except ValueError:
        return False

    if (time.time() - issued_at) / 86400 > SESSION_MAX_AGE_DAYS:
        return True

    try:
        keyring.set_password(_KEYRING_SERVICE, _SESSION_LAST_CHECK_KEY, today)
    except keyring.errors.KeyringError:
        pass
    return False


def check_auth(cookies) -> bool:
    if is_locally_expired():
        print(
            f'[auth] Local session age exceeds {SESSION_MAX_AGE_DAYS} days. Forcing re-login.',
            file=sys.stderr,
        )
        return False
    # Invalid/expired sessions still return status=0, but with an empty payload
    # and a Chinese error sid ("发生了一些问题，请重新登录。"). A valid session
    # returns a non-empty payload, so require that too.
    try:
        resp = send_gateway_request(cookies, 84, encode_message({1: '0'}))
        result = decode_response(resp['buffer'])
        packet = result.get('packet')
        if not packet:
            return False
        if str(packet.get('status', 0)) != '0':
            return False
        return bool(packet.get('payload'))
    except Exception:
        return False


# ==========================================
# QR Login
# ==========================================

def _is_remote_shell() -> bool:
    return any(os.environ.get(name) for name in _SSH_ENV_VARS)


def _can_auto_open_qr_image() -> bool:
    """Only auto-open Linux QR images in a local graphical session."""
    if sys.platform.startswith('linux'):
        if _is_remote_shell():
            return False
        return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))
    return True


def _open_qr_image(qr_image_path: str) -> bool:
    if not _can_auto_open_qr_image():
        return False
    try:
        if sys.platform == 'darwin':
            command = ['open', qr_image_path]
        elif sys.platform == 'win32':
            os.startfile(qr_image_path)  # type: ignore[attr-defined]
            return True
        else:
            command = ['xdg-open', qr_image_path]
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def _init_and_show_qr():
    init_resp = http_post_json(
        'https://login.feishu.cn/accounts/qrlogin/init',
        {'biz_type': None, 'redirect_uri': 'https://www.feishu.cn'},
        QR_LOGIN_HEADERS,
    )
    if init_resp['body'].get('code') != 0:
        raise RuntimeError(f"QR init failed: {init_resp['body']}")

    token = init_resp['body']['data']['step_info']['token']
    flow_key = init_resp['headers'].get('x-flow-key') or init_resp['headers'].get('X-Flow-Key', '')
    qr_content = f'{{"qrlogin":{{"token":"{token}"}}}}'

    # Method 1: Terminal ASCII QR code
    try:
        import qrcode
        qr = qrcode.QRCode()
        qr.add_data(qr_content)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        pass

    # Method 2: PNG image. Auto-open only where a local viewer is reliable;
    # remote Linux openers can block long enough for the QR token to expire.
    try:
        import qrcode as _qrcode
        os.makedirs(DATA_DIR, exist_ok=True)
        qr_image_path = os.path.join(DATA_DIR, 'login_qr.png')
        img = _qrcode.make(qr_content)
        img.save(qr_image_path)
        print(f'[login] QR image saved: {qr_image_path}', file=sys.stderr)
        if _open_qr_image(qr_image_path):
            print('[login] QR image viewer launched.', file=sys.stderr)
        elif sys.platform.startswith('linux'):
            print(
                '[login] Linux image auto-open skipped or unavailable; '
                'open the saved PNG if the terminal QR is hard to scan.',
                file=sys.stderr,
            )
    except Exception:
        pass

    print('[login] Scan the QR code with Feishu/Lark app.\n', file=sys.stderr)
    return token, flow_key


def _cleanup_qr_image():
    qr_image_path = os.path.join(DATA_DIR, 'login_qr.png')
    try:
        if not os.path.exists(qr_image_path):
            return
        # macOS only: try to close the Preview window we opened above.
        # On Linux/Windows we just delete the file; the viewer keeps the image
        # in memory and the user can close it when they want.
        if sys.platform == 'darwin':
            subprocess.run(
                ['osascript',
                 '-e', 'tell application "Preview"',
                 '-e', 'close every window',
                 '-e', 'quit',
                 '-e', 'end tell'],
                stderr=subprocess.DEVNULL,
            )
        os.unlink(qr_image_path)
    except Exception:
        pass


def cmd_login():
    MAX_RETRIES = 3
    for attempt in range(MAX_RETRIES):
        print('[login] Refreshing QR code...' if attempt > 0 else '[login] Generating QR code...', file=sys.stderr)
        token, flow_key = _init_and_show_qr()
        print('[login] Waiting for scan and confirmation...', file=sys.stderr)

        session = None
        expired = False
        poll_resp = None

        for _ in range(180):
            time.sleep(1)
            poll_resp = http_post_json(
                'https://login.feishu.cn/accounts/qrlogin/polling',
                {'biz_type': None},
                {**QR_LOGIN_HEADERS, 'x-flow-key': flow_key},
            )
            data = poll_resp['body'].get('data') if isinstance(poll_resp['body'], dict) else None
            if not data:
                continue

            status = data.get('step_info', {}).get('status')
            next_step = data.get('next_step')

            if status == 2:
                print('[login] QR scanned! Confirm on your phone...', file=sys.stderr)
            elif next_step == 'enter_app' or status == 4:
                # Confirmed — extract session cookie
                # 1. Try resp.cookies (most reliable with requests library)
                session = poll_resp.get('cookies', {}).get('session')
                # 2. Fall back to parsing Set-Cookie headers
                if not session:
                    import re
                    set_cookies = poll_resp['headers'].get('set-cookie') or poll_resp['headers'].get('Set-Cookie', '')
                    cookie_strs = set_cookies if isinstance(set_cookies, list) else ([set_cookies] if set_cookies else [])
                    for raw in cookie_strs:
                        m = re.search(r'(?:^|;\s*)session=([^;]+)', raw)
                        if m:
                            session = m.group(1)
                            break
                if not session:
                    print('[login] Warning: login confirmed but no session cookie in response.', file=sys.stderr)
                    print(f'[login] cookies: {poll_resp.get("cookies")}, set-cookie: {poll_resp["headers"].get("set-cookie")}', file=sys.stderr)
                break
            elif status == 3:
                print('[login] Canceled by user.', file=sys.stderr)
                break
            elif status == 5:
                expired = True
                break

        if session:
            save_session(session)
            # Try to save userId from login response
            try:
                login_data = poll_resp['body'].get('data', {}) if poll_resp else {}
                step_info = login_data.get('step_info', {})
                user_id = (step_info.get('front_user_id') or
                           login_data.get('user_id') or
                           step_info.get('user_id') or
                           login_data.get('uid'))
                if user_id:
                    save_user_id(str(user_id))
                    print(f'[login] userId {user_id} saved.', file=sys.stderr)
                else:
                    print('[login] No userId in login response, will resolve on first use.', file=sys.stderr)
            except Exception:
                pass
            _cleanup_qr_image()
            print('[login] Session saved successfully.', file=sys.stderr)
            import json
            print(json.dumps({'success': True}))
            return

        if expired and attempt < MAX_RETRIES - 1:
            print('[login] QR code expired, generating a new one...\n', file=sys.stderr)
            continue

        _cleanup_qr_image()
        import json
        print(json.dumps({'error': 'LOGIN_FAILED', 'message': 'No session cookie obtained.'}))
        sys.exit(1)
