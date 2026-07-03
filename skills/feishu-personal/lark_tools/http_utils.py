"""
http.py - HTTP helpers (mirrors lib/http.js)

Uses requests library; brotli decompression handled automatically when
the `brotli` package is installed.
"""

import time
import requests

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'


def cookies_to_dict(cookies: list) -> dict:
    return {c['name']: c['value'] for c in cookies}


def http_post_json(url: str, json_body: dict, extra_headers: dict = None) -> dict:
    """POST JSON body. Returns {body, headers, statusCode}."""
    headers = {**(extra_headers or {})}
    resp = requests.post(url, json=json_body, headers=headers, timeout=30, verify=True)
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    return {'body': body, 'headers': dict(resp.headers), 'cookies': dict(resp.cookies), 'statusCode': resp.status_code}


def sleep_ms(ms: int) -> None:
    time.sleep(ms / 1000)


def https_get(url: str, headers: dict = None) -> dict:
    """GET with redirect following. Returns {statusCode, buffer, headers}."""
    h = {'User-Agent': UA, **(headers or {})}
    resp = requests.get(url, headers=h, allow_redirects=True, timeout=30, verify=True)
    return {
        'statusCode': resp.status_code,
        'buffer': resp.content,
        'headers': dict(resp.headers),
    }


def http_get(cookies: list, hostname: str, url_path: str) -> dict:
    """GET with cookies. Returns {status, data, headers}."""
    url = f'https://{hostname}{url_path}'
    headers = {
        'User-Agent': UA,
        'Referer': f'https://{hostname}/',
        'Accept': 'application/json, text/plain, */*',
    }
    resp = requests.get(
        url,
        cookies=cookies_to_dict(cookies),
        headers=headers,
        allow_redirects=False,
        timeout=30,
        verify=True,
    )
    try:
        data = resp.json()
    except Exception:
        data = resp.text
    return {'status': resp.status_code, 'data': data, 'headers': dict(resp.headers)}


def http_post_with_cookies(cookies: list, hostname: str, url_path: str, json_body: dict,
                            extra_headers: dict = None) -> dict:
    """POST JSON with cookies. Returns {status, data, headers}.

    `extra_headers` lets callers add per-endpoint required headers (e.g.
    `Req-Version: 1` for whiteboard endpoints) without forking a new helper.
    """
    url = f'https://{hostname}{url_path}'
    headers = {
        'User-Agent': UA,
        'Referer': f'https://{hostname}/',
        'Accept': 'application/json, text/plain, */*',
        **(extra_headers or {}),
    }
    resp = requests.post(
        url,
        json=json_body,
        cookies=cookies_to_dict(cookies),
        headers=headers,
        allow_redirects=False,
        timeout=30,
        verify=True,
    )
    try:
        data = resp.json()
    except Exception:
        data = resp.text
    return {'status': resp.status_code, 'data': data, 'headers': dict(resp.headers)}


def http_post_binary(
    cookies: list,
    hostname: str,
    url_path: str,
    body: bytes,
    extra_headers: dict = None,
) -> dict:
    """POST raw bytes (Content-Type: application/octet-stream) with cookies.

    Used for docx image upload's merge_block step where the body is the raw
    file bytes and extra metadata rides in headers (x-seq-list etc.).
    """
    url = f'https://{hostname}{url_path}'
    headers = {
        'User-Agent': UA,
        'Referer': f'https://{hostname}/',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/octet-stream',
        **(extra_headers or {}),
    }
    resp = requests.post(
        url,
        data=body,
        cookies=cookies_to_dict(cookies),
        headers=headers,
        allow_redirects=False,
        timeout=120,
        verify=True,
    )
    try:
        data = resp.json()
    except Exception:
        data = resp.text
    return {'status': resp.status_code, 'data': data, 'headers': dict(resp.headers)}


def http_get_binary(cookies: list, url: str) -> dict:
    """GET binary with cookies and redirect following. Returns {status, buffer, contentType}."""
    headers = {
        'User-Agent': UA,
        'Referer': f'https://{url.split("/")[2]}/',
    }
    resp = requests.get(
        url,
        cookies=cookies_to_dict(cookies),
        headers=headers,
        allow_redirects=True,
        timeout=30,
        verify=True,
    )
    return {
        'status': resp.status_code,
        'buffer': resp.content,
        'contentType': resp.headers.get('content-type', ''),
    }
