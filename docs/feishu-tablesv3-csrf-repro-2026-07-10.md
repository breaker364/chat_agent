# Feishu `tablesv3` CSRF Token Error Reproduction

Date: 2026-07-10

## Target

- Wiki node: `BS6UwUhkVirgbwktavCcRbKUn9d`
- Base token resolved by the API: `ECDYbPjgra3Fv1s1pYQcjR7Ynip`
- Table: `tblmuUF7DcKdlUBV`
- View: `vewyEVXx9p`
- Session source: `sessionss/feishu_web_session.json`

No authentication credential values are recorded in this document.

## Successful Random Record

A single random record was successfully added to the table:

- Record ID: `rect57oiCCDRN`
- Field: `fldWXG2rOs` (`文本`, primary text field)
- Value: `csrf-repro-success-20260710-141123-6232d3d5`
- Verification: the record was returned by the records read endpoint.
- Record count after insertion: `31`

The successful write used `POST /space/api/rce/messages` with only the stored
`session` cookie and the minimal request headers used by `http_utils.py`.

## Reproduced Error

The error response is:

```text
HTTP 403
Content-Type: text/plain; charset=utf-8

csrf token error
```

It was reproduced on both:

- `POST /space/api/bitable/{base_token}/tablesv3/`
- `POST /space/api/rce/messages`

## Trigger Conditions

The important trigger is the request sequence and cookie state, not merely an
expired login session.

1. The saved session file contains only the `session` cookie.
2. A clean request using only that cookie can succeed.
3. A successful Feishu GET or POST can return additional cookies:
   - `sl_session`
   - `_csrf_token`
4. The persistent `requests.Session` automatically stores and sends those
   cookies on later requests.
5. Later POST requests can then return `403 csrf token error` because the
   client does not reproduce the complete browser CSRF protocol associated
   with those server-issued cookies.

Observed continuous `tablesv3` sequence in one persistent HTTP session:

| Request | Header variant | Result |
| --- | --- | --- |
| 1 | Minimal headers, stored `session` cookie | `200`, server sets `sl_session` and `_csrf_token` |
| 2 | Browser-style headers | `403 csrf token error` |
| 3-8 | Alternating minimal/browser headers | `403 csrf token error` |

The first successful request mutates the persistent cookie jar. The same
session value is therefore accepted for read operations while later POST
operations can fail CSRF validation.

## Header Findings

- Minimal headers can succeed when the HTTP cookie jar is clean.
- Adding `Origin`, web-version, command-version, terminal, OS, source, and
  locale headers does not reliably prevent the error.
- An exact wiki page `Referer` does not prevent the error.
- Copying the `_csrf_token` cookie value directly into an
  `x-csrf-token` header did not prevent the error.
- Removing the cookie `domain` attribute does not address the persistent
  session cookie state.
- Waiting two seconds and retrying does not address the persistent session
  cookie state.

## Existing Implementation Behavior

`skill_runner.py::_patch_requests_no_proxy()` creates one persistent
`requests.Session` and replaces the module-level request functions with that
session's methods. Response cookies therefore persist across API calls.

`commands/bitable.py::_bitable_post_with_csrf_fallback()` retries by:

1. Sending browser-style headers.
2. Removing cookie domain attributes.
3. Waiting two seconds and retrying.

It does not inspect or reset the persistent session cookie jar and does not
implement the complete Feishu browser CSRF exchange. After three failures it
reports that the stored session was invalidated, but this reproduction shows
that conclusion can be false: GET requests remained valid and a clean
session-only write succeeded.

`bitable_ot.py::submit_operations()` also assumes the response body is a JSON
object. When Feishu returns the plain-text CSRF response, the code raises:

```text
AttributeError: 'str' object has no attribute 'get'
```

This secondary exception hides the actual HTTP 403 unless the raw response is
captured.

## Reliable Reproduction Procedure

1. Load the stored `session` cookie into a new persistent `requests.Session`.
2. Send a normal Feishu GET such as the Bitable `clientvars` request.
3. Confirm the response cookie jar now contains `_csrf_token` and
   `sl_session`.
4. Send `POST /space/api/bitable/{base_token}/tablesv3/` without implementing
   the complete browser CSRF exchange.
5. Observe `HTTP 403` with body `csrf token error`.

The error may appear intermittent when every test uses a new HTTP session,
because the first clean request can succeed before Feishu's additional cookies
are persisted.

## Conclusion

This case is not evidence that the main `session` cookie is expired. It is a
CSRF state synchronization problem caused by persisting server-issued cookies
without reproducing the matching browser-side token protocol. A clean,
session-only request can work, while subsequent POST requests in the same
persistent HTTP session fail.

## Implemented Fix

The agent request wrapper now creates a fresh `requests.Session` for every
HTTP request while keeping `trust_env = False`. Explicit authentication
cookies are still sent, but response cookies such as `_csrf_token` and
`sl_session` cannot leak into later requests.

Additional changes:

- The Bitable `Origin` header is generated from the requested host instead of
  containing a tenant-specific hostname.
- The OT write path now reports non-JSON HTTP responses directly instead of
  raising `AttributeError` while parsing a plain-text error.
- A local regression test confirms that a response `_csrf_token` cookie is
  not sent on the next request.

Verification after the fix:

- Local Cookie-isolation regression test: passed.
- AST syntax check: passed.
- Real Agent `lark bitable schema` execution: passed.
- Three additional independent Agent processes all resolved the Wiki and
  completed without `csrf token error`.
- The real `tablesv3` response returned table schema with 6 fields and 31
  records.
