from __future__ import annotations

import base64
import io
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    if not payload:
        return False
    issued_at = float(payload.get("issued_at", 0))
    session = str(payload.get("session", "")).strip()
    if not session:
        return False
    return (time.time() - issued_at) < SESSION_MAX_AGE_SECONDS


def build_feishu_cookies(payload: dict[str, Any]) -> dict[str, str]:
    return {"session": str(payload["session"])}
