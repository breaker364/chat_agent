"""sheet_export.py — Server-side xlsx export for Feishu spreadsheets.

The old cmd_sheet_download reconstructed an xlsx from REST text values, which
silently lost cell types (numbers became strings), formulas, sheet metadata,
and styling (numberFormat was not preserved). This module uses Feishu's native
export pipeline so the downloaded xlsx is server-rendered instead of rebuilt by
openpyxl. Values, formulas, sheet names, merged cells, freeze panes, and normal
workbook formatting match the browser export observed in live checks.

Note: browser downloads can include collaboration comments / threaded comments
depending on the UI path/options. The export endpoint below has been observed
to omit those comment parts, so the bytes are not guaranteed to be identical to
every file downloaded from the browser UI.

Wire format (reverse-engineered 2026-05-18 via Playwright XHR hook on
the browser's download flow):

  1. POST https://nio.feishu.cn/space/api/export/create/
     body: {"token": <obj_token>, "type": "sheet",
            "file_extension": "xlsx", "event_source": "6"}
     resp: {"code": 0, "data": {"ticket": <id>, "job_timeout": 600}}

  2. GET  https://nio.feishu.cn/space/api/export/result/<ticket>?token=<t>&type=sheet
     poll until result.job_status == 0
     resp: {"data": {"result": {"job_status": 0,
                                "file_token": <download_token>,
                                "file_extension": "xlsx",
                                "file_name": ..., "file_size": N,
                                ...}}}

  3. GET  https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/all/<file_token>
     resp: binary xlsx (Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet)

Note: step 3 uses a different host (internal-api-drive-stream.feishu.cn,
same one as image downloads) — the production CLI's http layer already
handles this host via http_get_binary().
"""

import time

from .config import DOC_HOST, DOC_IMAGE_HOST
from .http_utils import http_get, http_get_binary, http_post_with_cookies


_DEFAULT_POLL_INTERVAL = 1.0
_DEFAULT_TIMEOUT = 60.0


def _create_export_ticket(cookies, spreadsheet_token: str) -> str:
    body = {
        'token': spreadsheet_token,
        'type': 'sheet',
        'file_extension': 'xlsx',
        # event_source=6 is the observed value for sheet xlsx export; other
        # values are unverified. Server doesn't seem to validate strictly.
        'event_source': '6',
    }
    res = http_post_with_cookies(cookies, DOC_HOST, '/space/api/export/create/', body)
    payload = (res or {}).get('data') or {}
    if payload.get('code') != 0:
        raise RuntimeError(
            f"export/create failed: code={payload.get('code')} msg={payload.get('msg')!r}"
        )
    ticket = (payload.get('data') or {}).get('ticket')
    if not ticket:
        raise RuntimeError(f'export/create returned no ticket: {payload!r}')
    return str(ticket)


def _poll_export_result(cookies, ticket: str, spreadsheet_token: str,
                        *, poll_interval: float, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last_status = None
    while True:
        path = (
            f'/space/api/export/result/{ticket}'
            f'?token={spreadsheet_token}&type=sheet'
        )
        res = http_get(cookies, DOC_HOST, path)
        payload = (res or {}).get('data') or {}
        if payload.get('code') != 0:
            raise RuntimeError(
                f"export/result failed: code={payload.get('code')} msg={payload.get('msg')!r}"
            )
        result = ((payload.get('data') or {}).get('result')) or {}
        last_status = result.get('job_status')
        # job_status: 0 = success, anything else still pending or failed.
        # job_error_msg carries the human-readable status.
        if last_status == 0:
            file_token = result.get('file_token')
            if not file_token:
                raise RuntimeError(f'export/result missing file_token: {result!r}')
            return result
        # Treat status != 0 as still-pending unless the deadline is hit.
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f'export polling timed out after {timeout}s '
                f'(last job_status={last_status}, msg={result.get("job_error_msg")!r})'
            )
        time.sleep(poll_interval)


def _download_export_bytes(cookies, file_token: str) -> bytes:
    url = (
        f'https://{DOC_IMAGE_HOST}'
        f'/space/api/box/stream/download/all/{file_token}'
    )
    res = http_get_binary(cookies, url)
    status = res.get('status')
    if status != 200:
        raise RuntimeError(
            f'export download failed: status={status} '
            f'ct={res.get("contentType")!r}'
        )
    buf = res.get('buffer') or b''
    if not buf:
        raise RuntimeError('export download returned empty body')
    return buf


def export_sheet_xlsx(cookies, spreadsheet_token: str,
                      *, poll_interval: float = _DEFAULT_POLL_INTERVAL,
                      timeout: float = _DEFAULT_TIMEOUT) -> bytes:
    """Run the 3-step export flow and return the xlsx bytes.

    Output is a full multi-sheet workbook server-rendered with cell types,
    formulas, sheet names, merged cells, freeze panes, and numberFormat
    preserved. Callers wanting a single sheet should post-process with
    openpyxl.

    Raises RuntimeError on any step failure (HTTP error, polling timeout,
    empty body). Default timeout is 60s — small sheets export in <1s, but
    large multi-tab books may need longer; bump via the `timeout` kwarg.
    """
    ticket = _create_export_ticket(cookies, spreadsheet_token)
    result = _poll_export_result(
        cookies, ticket, spreadsheet_token,
        poll_interval=poll_interval, timeout=timeout,
    )
    return _download_export_bytes(cookies, result['file_token'])
