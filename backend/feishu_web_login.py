from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import qrcode
import requests

from .config import get_runtime_value

LOGIN_INIT_URL = "https://login.feishu.cn/accounts/qrlogin/init"
LOGIN_POLL_URL = "https://login.feishu.cn/accounts/qrlogin/polling"
DEFAULT_REDIRECT_URL = "https://www.feishu.cn"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600

QR_LOGIN_HEADERS = {
    "x-api-version": "1.0.8",
    "x-app-id": "2",
    "x-device-info": "device_id=0;device_name=chat-agent;device_os=Windows",
    "x-locale": "zh-CN",
    "x-terminal-type": "2",
}


@dataclass
class FeishuQrLoginState:
    token: str
    flow_key: str
    qr_content: str
    qr_png_base64: str
    created_at: float


class FeishuWebSessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        session_dir = str(get_runtime_value("paths", "session_dir", "sessionss") or "sessionss")
        session_file = str(
            get_runtime_value("paths", "feishu_session_file", "feishu_web_session.json")
            or "feishu_web_session.json"
        )
        self.path = self.root / session_dir / session_file
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, payload: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self) -> None:
        if self.path.exists():
            try:
                self.path.unlink()
            except PermissionError:
                self.path.write_text("{}", encoding="utf-8")


def _extract_session_cookie(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    match = re.search(r"(?:^|;\s*)session=([^;]+)", raw)
    return (match.group(1) if match else raw).strip()


def _configured_session_cookie() -> tuple[str, str]:
    candidates = [
        ("env:FEISHU_SESSION", os.environ.get("FEISHU_SESSION", "")),
        ("env:FEISHU_SESSION_COOKIE", os.environ.get("FEISHU_SESSION_COOKIE", "")),
        ("runtime_config:feishu.session", get_runtime_value("feishu", "session", "")),
        ("runtime_config:feishu.session_cookie", get_runtime_value("feishu", "session_cookie", "")),
        ("runtime_config:feishu.web_session", get_runtime_value("feishu", "web_session", "")),
    ]
    for source, value in candidates:
        session = _extract_session_cookie(str(value or ""))
        if session:
            return session, source
    return "", ""


def feishu_session_status_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    session = str((payload or {}).get("session") or "").strip()
    issued_at = float((payload or {}).get("issued_at") or 0)
    has_session = bool(session)
    age_seconds = max(0, time.time() - issued_at) if issued_at else None
    expired = bool(has_session and issued_at and age_seconds is not None and age_seconds >= SESSION_MAX_AGE_SECONDS)
    logged_in = is_feishu_session_valid(payload)
    reason = ""
    if not has_session:
        reason = "missing_session"
    elif not issued_at:
        reason = "missing_issued_at"
    elif expired:
        reason = "expired"
    elif logged_in:
        reason = "valid"
    else:
        reason = "invalid"
    return {
        "logged_in": logged_in,
        "has_session": has_session,
        "issued_at": issued_at or None,
        "age_seconds": age_seconds,
        "max_age_seconds": SESSION_MAX_AGE_SECONDS,
        "reason": reason,
        "metadata": (payload or {}).get("metadata", {}) if isinstance(payload, dict) else {},
    }


def bootstrap_feishu_session(store: FeishuWebSessionStore) -> dict[str, Any]:
    """Load persisted Feishu session, or seed it from config/env if missing or expired."""
    existing = store.load()
    existing_status = feishu_session_status_payload(existing)
    if existing_status["logged_in"]:
        return {
            **existing_status,
            "bootstrapped": False,
            "source": existing_status.get("metadata", {}).get("source", "session_file"),
            "path": str(store.path),
        }

    configured_session, source = _configured_session_cookie()
    if configured_session:
        payload = {
            "session": configured_session,
            "issued_at": time.time(),
            "metadata": {
                "source": source,
                "bootstrapped_at": time.time(),
                "previous_reason": existing_status.get("reason"),
            },
        }
        store.save(payload)
        return {
            **feishu_session_status_payload(payload),
            "bootstrapped": True,
            "source": source,
            "path": str(store.path),
        }

    return {
        **existing_status,
        "bootstrapped": False,
        "source": "none",
        "path": str(store.path),
    }


def _make_qr_png_base64(content: str) -> str:
    img = qrcode.make(content)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def init_feishu_qr_login() -> FeishuQrLoginState:
    resp = requests.post(
        LOGIN_INIT_URL,
        json={"biz_type": None, "redirect_uri": DEFAULT_REDIRECT_URL},
        headers=QR_LOGIN_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"QR init failed: {payload}")
    token = payload["data"]["step_info"]["token"]
    flow_key = resp.headers.get("x-flow-key", "")
    qr_content = json.dumps({"qrlogin": {"token": token}}, ensure_ascii=False)
    return FeishuQrLoginState(
        token=token,
        flow_key=flow_key,
        qr_content=qr_content,
        qr_png_base64=_make_qr_png_base64(qr_content),
        created_at=time.time(),
    )


def _extract_session_from_response(resp: requests.Response) -> str | None:
    session = resp.cookies.get("session")
    if session:
        return session
    raw = resp.headers.get("Set-Cookie", "") or resp.headers.get("set-cookie", "")
    match = re.search(r"(?:^|;\s*)session=([^;]+)", raw)
    return match.group(1) if match else None


def poll_feishu_qr_login(flow_key: str) -> dict[str, Any]:
    resp = requests.post(
        LOGIN_POLL_URL,
        json={"biz_type": None},
        headers={**QR_LOGIN_HEADERS, "x-flow-key": flow_key},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json().get("data", {})
    step_info = payload.get("step_info", {})
    status = step_info.get("status")
    next_step = payload.get("next_step")
    session = None
    if next_step == "enter_app" or status == 4:
        session = _extract_session_from_response(resp)
    return {
        "status": status,
        "next_step": next_step,
        "session": session,
        "raw": payload,
    }


def is_feishu_session_valid(payload: dict[str, Any] | None) -> bool:
    """Local timestamp-based check only. Fast but may give false positives.

    For a real server-side check, use is_feishu_session_server_valid().
    """
    if not payload:
        return False
    issued_at = float(payload.get("issued_at", 0))
    session = str(payload.get("session", "")).strip()
    if not session:
        return False
    return (time.time() - issued_at) < SESSION_MAX_AGE_SECONDS


def _resolve_feishu_probe_url(probe_url: str | None = None) -> str:
    configured = (
        probe_url
        or os.environ.get("FEISHU_WEB_URL")
        or os.environ.get("FEISHU_DOC_HOST")
        or DEFAULT_REDIRECT_URL
    ).strip()
    if "://" not in configured:
        configured = f"https://{configured}"
    parsed = urlparse(configured)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not (
        hostname == "feishu.cn" or hostname.endswith(".feishu.cn")
    ):
        raise ValueError("Feishu probe URL must use an https feishu.cn host")
    return configured if configured.endswith("/") else f"{configured}/"


def is_feishu_session_server_valid(
    payload: dict[str, Any] | None,
    timeout: int = 10,
    probe_url: str | None = None,
) -> dict[str, Any]:
    """Verify the session against the Feishu server with a real HTTP request.

    Probes a configurable Feishu web URL with the stored session cookie and
    follows up to 2 redirects. An invalid/expired session is redirected to
    the account login page.

    Returns a dict with keys:
      - valid: bool — whether the session is accepted by the server
      - reason: str — human-readable status
      - status_code: int | None — final HTTP status from the probe chain
      - final_url: str | None — URL the probe landed on
    """
    if not payload:
        return {"valid": False, "reason": "no_payload", "status_code": None, "final_url": None}
    session = str(payload.get("session", "")).strip()
    if not session:
        return {"valid": False, "reason": "empty_session_cookie", "status_code": None, "final_url": None}

    try:
        initial_url = _resolve_feishu_probe_url(probe_url)
    except ValueError as exc:
        return {
            "valid": False,
            "reason": f"server_probe_invalid_url: {exc}",
            "status_code": None,
            "final_url": None,
        }

    session_cookies = {"session": session}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html, */*",
    }

    try:
        resp = requests.get(
            initial_url,
            cookies=session_cookies,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.Timeout:
        return {"valid": False, "reason": f"server_probe_timed_out_after_{timeout}s", "status_code": None, "final_url": None}
    except requests.ConnectionError as exc:
        return {"valid": False, "reason": f"server_probe_connection_failed: {exc}", "status_code": None, "final_url": None}
    except Exception as exc:
        return {"valid": False, "reason": f"server_probe_error: {exc}", "status_code": None, "final_url": None}

    # Step 2: follow the redirect chain (up to 2 hops).
    current_url = initial_url
    final_status = resp.status_code
    location = resp.headers.get("Location") or resp.headers.get("location") or ""
    final_url = urljoin(current_url, location) if location else current_url

    for _ in range(2):
        if not location or final_status not in (301, 302, 307, 308):
            break
        current_url = urljoin(current_url, location)
        try:
            next_resp = requests.get(
                current_url,
                cookies=session_cookies,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
        except Exception:
            break
        final_status = next_resp.status_code
        location = next_resp.headers.get("Location") or next_resp.headers.get("location") or ""
        final_url = urljoin(current_url, location) if location else current_url

        # Check if this redirect targets the login page
        loc_lower = final_url.lower()
        if "accounts" in loc_lower or "passport" in loc_lower or "login" in loc_lower:
            return {
                "valid": False,
                "reason": f"server_redirected_to_login: {final_url}",
                "status_code": final_status,
                "final_url": final_url,
            }

        if final_status not in (301, 302, 307, 308):
            break

    if final_status == 200:
        return {
            "valid": True,
            "reason": "server_accepted_session",
            "status_code": final_status,
            "final_url": final_url,
        }
    return {
        "valid": False,
        "reason": f"server_returned_unexpected_status_{final_status}",
        "status_code": final_status,
        "final_url": final_url,
    }


def build_feishu_cookies(payload: dict[str, Any]) -> dict[str, str]:
    return {"session": str(payload["session"])}
