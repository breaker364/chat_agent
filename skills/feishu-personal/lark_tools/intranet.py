"""
intranet.py — Compliance gate: refuse to run unless on company intranet.

Per security review (2026-04-28), the lark CLI must only run from inside the
company network. This module performs a three-layer check:

  L1 — DNS resolution of an intranet-only host must succeed.
  L2 — The resolved IP must be in an RFC1918 private range
       (defeats /etc/hosts → public IP spoofing).
  L3 — TCP connect + HTTPS handshake + certificate validation against the
       OS trust store (Keychain / Windows cert store / ca-certificates),
       with hostname verification (defeats LAN-spoof attacks unless the
       attacker holds a valid cert for the probe host — which would require
       compromising the company CA).

We route ssl validation through the OS trust store via `truststore` so users
don't need their Python certifi bundle to mirror the company internal CA.
Missing dep or too-old Python → exit 3 (deliberately loud — silent fallback
to certifi just re-creates the old "BLOCKED but actually on intranet" pain).
Python ≥3.10 is required because truststore depends on ssl-module hooks added
in 3.10.
"""

import ipaddress
import socket
import ssl
import sys
import time

PROBE_HOST = 'git.nevint.com'
PROBE_PORT = 443
PROBE_TIMEOUT = 3.0
RETRY_DELAY_SEC = 0.5

MIN_PYTHON = (3, 10)


def _assert_python_version() -> None:
    if sys.version_info >= MIN_PYTHON:
        return
    have = f'{sys.version_info.major}.{sys.version_info.minor}'
    need = f'{MIN_PYTHON[0]}.{MIN_PYTHON[1]}'
    sys.stderr.write(
        f'[lark] Python {have} is too old; need ≥{need}.\n'
        '       Upgrade: brew install python@3.12   (macOS)\n'
        '                pyenv install 3.12         (any platform)\n'
    )
    sys.exit(3)


def _apply_os_trust_bridge() -> None:
    try:
        import truststore
    except ImportError:
        sys.stderr.write(
            '[lark] missing dependency: truststore\n'
            '       Run: pip install -e .  (or: pip install "truststore>=0.9")\n'
        )
        sys.exit(3)
    truststore.inject_into_ssl()


_assert_python_version()
_apply_os_trust_bridge()


def _check() -> None:
    """Raises RuntimeError or one of the socket/ssl exceptions on failure."""
    # L1: DNS must resolve.
    ip = socket.gethostbyname(PROBE_HOST)

    # L2: Resolved IP must be private (RFC1918 / link-local / etc.).
    if not ipaddress.ip_address(ip).is_private:
        raise RuntimeError(
            f'resolved IP {ip} for {PROBE_HOST} is not in a private range'
        )

    # L3: TCP connect + TLS handshake with cert + hostname validation.
    # `create_default_context()` enforces full chain validation against the
    # system trust store; `wrap_socket(server_hostname=...)` enforces SAN/CN
    # match. A handshake that returns without raising is sufficient evidence
    # that we are talking to the real intranet host.
    ctx = ssl.create_default_context()
    with socket.create_connection((ip, PROBE_PORT), timeout=PROBE_TIMEOUT) as sock:
        with ctx.wrap_socket(sock, server_hostname=PROBE_HOST):
            pass


def assert_on_intranet() -> None:
    """Block CLI execution unless the three-layer probe passes.

    Called from cli.main() at the earliest point so every subcommand
    (including `test-cmd` and `--help`'s sibling subcommands) is gated
    uniformly. Retries once after a short delay to absorb transient
    network blips (DNS hiccups, momentary TLS errors) without weakening
    the security gate — only sustained failure across two attempts
    triggers BLOCKED.
    """
    try:
        _check()
        return
    except Exception:
        pass

    time.sleep(RETRY_DELAY_SEC)

    try:
        _check()
    except Exception:
        sys.stderr.write(
            '[lark] BLOCKED: 检测到非公司内网环境，禁止使用。\n'
            '       按信息安全要求，本工具仅限在公司内网（含 VPN）使用，\n'
            '       禁止在外网运行；违规使用可能导致敏感信息泄露。\n'
            '       请连接公司 VPN 后重试。\n'
            '\n'
            '       如果你确认已在内网/VPN 内仍然报错：CLI 已通过\n'
            '       truststore 走 OS 信任库，大概率是公司根证书没被 IT\n'
            '       推到本机系统证书库（Keychain / Windows 证书存储 /\n'
            '       Linux ca-certificates）。详见 README "故障排查" 一节。\n'
        )
        sys.exit(2)
