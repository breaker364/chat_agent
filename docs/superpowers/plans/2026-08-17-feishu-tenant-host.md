# Feishu Tenant Host Generalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove fixed tenant-host assumptions from Feishu runtime code so personal `*.feishu.cn` resource URLs work end-to-end.

**Architecture:** Parse and validate the tenant hostname at each resource URL boundary, then pass it explicitly through the command and HTTP layers. Keep a configurable public host as the fallback for bare tokens and account-scoped commands. The backend session probe accepts the same configurable base URL and resolves relative redirects against the actual probe host.

**Tech Stack:** Python 3.10+, `requests`, `pytest`, existing `lark_tools` CLI, FastAPI backend session store.

## Global Constraints

- Do not hard-code a concrete tenant or entity name in runtime code.
- Accept only `feishu.cn` or a subdomain of it as a resource host.
- Default the configurable resource host to `www.feishu.cn` when no environment/config value is supplied.
- Preserve existing CLI arguments and existing result shapes wherever possible by adding optional host parameters.
- Do not read, print, commit, or transmit the session value from `sessionss/feishu_web_session.json`.
- Leave historical documentation, captured request records, and fixed historical result fixtures unchanged.
- Use TDD: every production change must follow a test that was observed failing.

---

### Task 1: Add centralized Feishu host parsing and unit tests

**Files:**
- Modify: `skills/feishu-personal/lark_tools/config.py:1-20`
- Create: `skills/feishu-personal/test/test_host_resolution.py`

**Interfaces:**
- Produces `DEFAULT_FEISHU_HOST`, `resolve_feishu_host(reference: str | None = None, default: str | None = None) -> str`, and `feishu_url(host: str | None, path: str) -> str`.
- `resolve_feishu_host` accepts a full URL, a hostname, or `None`; it strips scheme/path/query/fragment and rejects explicit hosts outside `feishu.cn` and its subdomains with `ValueError`.
- `DOC_HOST` and `BITABLE_HOST` remain import-compatible aliases of the validated default host.

- [ ] **Step 1: Write the failing tests**

```python
import pytest


def test_resolve_feishu_host_extracts_personal_tenant_from_resource_url():
    from lark_tools.config import resolve_feishu_host

    assert resolve_feishu_host(
        "https://dcnhd2xewyfq.feishu.cn/wiki/BwdawnYQDixE21ketifc5rAJnJg?tab=tbl"
    ) == "dcnhd2xewyfq.feishu.cn"


def test_resolve_feishu_host_uses_configured_default_for_bare_token():
    from lark_tools.config import resolve_feishu_host

    assert resolve_feishu_host(None, default="www.feishu.cn") == "www.feishu.cn"


def test_resolve_feishu_host_rejects_external_host():
    from lark_tools.config import resolve_feishu_host

    with pytest.raises(ValueError, match="feishu.cn"):
        resolve_feishu_host("https://example.invalid/wiki/token")


def test_feishu_url_normalizes_path_and_uses_requested_host():
    from lark_tools.config import feishu_url

    assert feishu_url("dcnhd2xewyfq.feishu.cn", "space/api/meta/?token=x") == (
        "https://dcnhd2xewyfq.feishu.cn/space/api/meta/?token=x"
    )
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `python -m pytest test/test_host_resolution.py -q`

Expected: FAIL because `resolve_feishu_host` and `feishu_url` do not exist.

- [ ] **Step 3: Implement the smallest host helper**

In `config.py`, use `urllib.parse.urlparse`, `os.environ.get("FEISHU_DOC_HOST")`, and a hostname suffix check. Normalize a host with `lower().rstrip(".")`; accept `feishu.cn` and values ending in `.feishu.cn`; reject empty or external explicit hosts. Build URLs with exactly one slash between host and path.

- [ ] **Step 4: Run focused tests and verify they pass**

Run: `python -m pytest test/test_host_resolution.py -q`

Expected: 4 passed.

- [ ] **Step 5: Commit the nested-repository unit**

Run from `skills/feishu-personal`: `git add lark_tools/config.py test/test_host_resolution.py; git commit -m "feat: resolve Feishu tenant hosts generically"`

Expected: a commit containing only the host helper and its tests.

### Task 2: Generalize backend Feishu session server validation

**Files:**
- Modify: `backend/feishu_web_login.py:1-20,238-335`
- Create: `backend/tests/test_feishu_web_login.py`

**Interfaces:**
- Extend `is_feishu_session_server_valid(payload, timeout=10, probe_url=None) -> dict` without breaking existing callers.
- If `probe_url` is omitted, derive `https://<validated default host>/` from `FEISHU_WEB_URL` or `FEISHU_DOC_HOST`, falling back to the public Feishu host.
- Relative `Location` values resolve with `urllib.parse.urljoin` against the current URL; no tenant literal is used.

- [ ] **Step 1: Write failing redirect-chain tests**

Patch `backend.feishu_web_login.requests.get` with a real `Response`-like `Mock` sequence and assert:

```python
def test_session_probe_uses_requested_personal_host_and_resolves_relative_redirect(monkeypatch):
    from backend.feishu_web_login import is_feishu_session_server_valid

    first = Mock(status_code=302, headers={"Location": "/drive/home/"})
    second = Mock(status_code=200, headers={})
    get = Mock(side_effect=[first, second])
    monkeypatch.setattr("backend.feishu_web_login.requests.get", get)

    result = is_feishu_session_server_valid(
        {"session": "redacted", "issued_at": time.time()},
        probe_url="https://dcnhd2xewyfq.feishu.cn/",
    )

    assert result["valid"] is True
    assert get.call_args_list[0].args[0] == "https://dcnhd2xewyfq.feishu.cn/"
    assert get.call_args_list[1].args[0] == "https://dcnhd2xewyfq.feishu.cn/drive/home/"


def test_session_probe_marks_login_redirect_invalid(monkeypatch):
    from backend.feishu_web_login import is_feishu_session_server_valid

    response = Mock(status_code=302, headers={"Location": "https://accounts.feishu.cn/login"})
    monkeypatch.setattr("backend.feishu_web_login.requests.get", Mock(return_value=response))

    result = is_feishu_session_server_valid(
        {"session": "redacted", "issued_at": time.time()},
        probe_url="https://dcnhd2xewyfq.feishu.cn/",
    )

    assert result["valid"] is False
    assert "login" in result["reason"]
```

- [ ] **Step 2: Run the backend tests and verify the new tests fail**

Run: `python -m pytest backend/tests/test_feishu_web_login.py -q`

Expected: FAIL because `probe_url` is not accepted and the old implementation joins relative redirects with a fixed tenant.

- [ ] **Step 3: Implement configurable probe URL and safe redirect joining**

Add a small helper that validates the configured URL hostname with the same `feishu.cn` suffix rule, use `urljoin` for relative redirects, and preserve the existing timeout/connection/error result keys. Keep the session string out of all return values and logs.

- [ ] **Step 4: Run the backend tests and verify they pass**

Run: `python -m pytest backend/tests/test_feishu_web_login.py -q`

Expected: 2 passed.

- [ ] **Step 5: Commit the backend unit**

Run: `git add backend/feishu_web_login.py backend/tests/test_feishu_web_login.py; git commit -m "fix: make Feishu session probe host configurable"`

### Task 3: Propagate host through inspect and read-side resource resolvers

**Files:**
- Modify: `skills/feishu-personal/lark_tools/commands/inspect.py`
- Modify: `skills/feishu-personal/lark_tools/commands/bitable.py`
- Modify: `skills/feishu-personal/lark_tools/commands/sheet.py`
- Modify: `skills/feishu-personal/lark_tools/commands/minutes.py`
- Modify: `skills/feishu-personal/lark_tools/wiki_tree.py`
- Modify: `skills/feishu-personal/test/test_url_parsing.py`
- Modify: `skills/feishu-personal/test/test_inspect.py`

**Interfaces:**
- URL resolvers retain their existing token fields and add a `host` field where they already return dictionaries.
- `inspect_input` derives the host once from `raw` and passes it to `_probe_meta` and `_resolve_wiki_node`; every returned canonical URL uses that host.
- Read fetchers add `host: str | None = None` after existing positional parameters and fall back to `resolve_feishu_host()`.

- [ ] **Step 1: Add failing tests for personal-host propagation**

Extend the resolver tests with:

```python
def test_bitable_url_resolution_preserves_tenant_host():
    from lark_tools.commands.bitable import resolve_bitable_token

    result = resolve_bitable_token(
        "https://dcnhd2xewyfq.feishu.cn/base/appABC123?table=tblAAA"
    )

    assert result["token"] == "appABC123"
    assert result["host"] == "dcnhd2xewyfq.feishu.cn"


def test_inspect_uses_url_host_for_wiki_lookup_and_output():
    from lark_tools.commands.inspect import inspect_input

    calls = []

    def fake_get(cookies, host, path):
        calls.append((host, path))
        return {"data": {"code": 0, "data": {"obj_token": "doxABC", "obj_type": 22, "title": "T"}}}

    result = inspect_input(
        fake_get,
        [{"name": "session", "value": "redacted"}],
        "https://dcnhd2xewyfq.feishu.cn/wiki/wikiABC",
    )

    assert calls[0][0] == "dcnhd2xewyfq.feishu.cn"
    assert result["url"].startswith("https://dcnhd2xewyfq.feishu.cn/")
```

- [ ] **Step 2: Run the focused resolver tests and verify they fail**

Run: `python -m pytest test/test_url_parsing.py test/test_inspect.py -q`

Expected: FAIL because resolver results lack `host` and inspect still calls the configured fixed host.

- [ ] **Step 3: Implement host propagation for read-side resolvers**

Import `resolve_feishu_host` into the five resource modules. Parse URL hostname at the boundary, add optional host parameters to HTTP helpers, and use the host for wiki-node lookup, metadata probes, canonical URLs, sheet reads, bitable reads, minute URLs, and wiki-tree requests. Do not mutate `DOC_HOST` or `BITABLE_HOST`.

- [ ] **Step 4: Run focused resolver tests and the existing URL/inspect tests**

Run: `python -m pytest test/test_url_parsing.py test/test_inspect.py -q`

Expected: all selected tests pass with no warnings or errors.

- [ ] **Step 5: Commit read-side propagation**

Run from `skills/feishu-personal`: `git add lark_tools/commands/inspect.py lark_tools/commands/bitable.py lark_tools/commands/sheet.py lark_tools/commands/minutes.py lark_tools/wiki_tree.py test/test_url_parsing.py test/test_inspect.py; git commit -m "fix: propagate tenant host through resource resolvers"`

### Task 4: Propagate host through document, sheet, bitable, wiki, whiteboard, and file operations

**Files:**
- Modify: `skills/feishu-personal/lark_tools/commands/doc.py`
- Modify: `skills/feishu-personal/lark_tools/commands/doc_write.py`
- Modify: `skills/feishu-personal/lark_tools/commands/bitable_write.py`
- Modify: `skills/feishu-personal/lark_tools/commands/sheet_write.py`
- Modify: `skills/feishu-personal/lark_tools/commands/wiki.py`
- Modify: `skills/feishu-personal/lark_tools/commands/whiteboard_write.py`
- Modify: `skills/feishu-personal/lark_tools/commands/img.py`
- Modify: `skills/feishu-personal/lark_tools/commands/file.py`
- Modify: `skills/feishu-personal/lark_tools/docx_ot.py`
- Modify: `skills/feishu-personal/lark_tools/sheet_export.py`
- Modify: `skills/feishu-personal/lark_tools/sheet_xlsx_import.py`
- Modify: `skills/feishu-personal/lark_tools/whiteboard_api.py`
- Modify: `skills/feishu-personal/lark_tools/whiteboard_ot.py`
- Modify: `skills/feishu-personal/lark_tools/sheet_ot.py`
- Modify: `skills/feishu-personal/lark_tools/wiki_delete.py`
- Modify: `skills/feishu-personal/test/test_host_resolution.py`

**Interfaces:**
- Every resource-scoped command obtains `host = resolve_feishu_host(token_or_url)` before token extraction and passes it to all nested HTTP/URL-building helpers.
- Existing helper signatures gain a trailing optional `host` parameter, preserving direct callers.
- Static `https://<fixed-tenant>/...` constructions are replaced with `feishu_url(host, path)` or the current call's host.
- `commands/img.py` and `commands/file.py` use the resource host for `Origin`/`Referer`; platform upload/download hosts remain unchanged.

- [ ] **Step 1: Add failing request-routing tests**

Add tests that monkeypatch the existing HTTP helpers and assert that `cmd_doc_meta`/`cmd_bitable_schema` with a personal resource URL send the personal hostname, and that `parse_inline_markdown` with a bare document token generates a URL using the configured default without a concrete tenant host. Add a header test asserting `send_gateway_request(..., host="dcnhd2xewyfq.feishu.cn")` emits matching `Origin` and `Referer`.

- [ ] **Step 2: Run the focused request-routing tests and verify they fail**

Run: `python -m pytest test/test_host_resolution.py test/test_url_parsing.py -q`

Expected: FAIL because document/bitable helpers and gateway headers still use module defaults or fixed URL literals.

- [ ] **Step 3: Implement explicit host arguments across resource operations**

Thread the parsed host through document read/write helpers, sheet and bitable mutations, wiki tree operations, whiteboard operations, export/import flows, image/file headers, and generated resource URLs. Use default host only for commands without a resource URL or bare tokens. Preserve command output keys and do not alter platform service hosts.

- [ ] **Step 4: Run focused and package tests**

Run: `python -m pytest test tests -q`

Expected: the full Feishu skill test set passes. If an existing mock asserts an old call signature, update the mock to accept the new optional keyword while preserving the behavior being tested.

- [ ] **Step 5: Commit resource operation propagation**

Run from `skills/feishu-personal`: `git add lark_tools test; git commit -m "fix: route Feishu resource operations by tenant host"`

### Task 5: Generalize gateway origin and remove runtime fixed-tenant literals

**Files:**
- Modify: `skills/feishu-personal/lark_tools/gateway.py`
- Modify: `skills/feishu-personal/lark_tools/auth.py`
- Modify: `skills/feishu-personal/skill_runner.py`
- Modify: `skills/feishu-personal/lark_tools/commands/doc.py`
- Modify: `skills/feishu-personal/lark_tools/commands/whiteboard_write.py`
- Modify: `skills/feishu-personal/lark_tools/sheet_xlsx_import.py`
- Modify: `skills/feishu-personal/lark_tools/sheet_export.py`

**Interfaces:**
- `send_gateway_request(cookies, cmd, payload, host: str | None = None)` uses `feishu_url(host, "/")` for `Origin` and `Referer`.
- `auth` passes an optional host where a caller has one and uses the configured default for account-scoped checks.
- Generated links never contain a concrete tenant host literal; tests may use a deterministic personal host fixture.

- [ ] **Step 1: Add failing gateway and source-scan tests**

Test the headers with a fake `requests.post`, and add a source scan over runtime Python files that asserts no occurrence of the removed fixed tenant string remains outside tests or historical documentation.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python -m pytest test/test_host_resolution.py -q`

Expected: FAIL on the old `Origin`/`Referer` values and the source scan.

- [ ] **Step 3: Implement gateway and literal cleanup**

Use the centralized host helper in gateway/auth and replace fixed-tenant runtime URL construction with the current/default host. Keep authentication endpoints and platform service endpoints unchanged.

- [ ] **Step 4: Run package tests and runtime source scan**

Run: `python -m pytest test tests -q; rg -n --glob '*.py' 'nio\\.feishu\\.cn' lark_tools`

Expected: all tests pass and the source scan returns no matches in runtime Python files.

- [ ] **Step 5: Commit gateway and literal cleanup**

Run from `skills/feishu-personal`: `git add lark_tools/auth.py lark_tools/gateway.py skill_runner.py lark_tools/commands/doc.py lark_tools/commands/whiteboard_write.py lark_tools/sheet_xlsx_import.py lark_tools/sheet_export.py test; git commit -m "fix: remove fixed tenant host assumptions"`

### Task 6: Run full verification and read-only personal-tenant integration check

**Files:**
- Modify: `skills/feishu-personal/test/test_live_resources.py` only if an existing test needs a host parameter; do not add the session value or a new secret fixture.
- Modify: none for the integration command itself.

**Interfaces:**
- The existing session file remains outside Git and is read only by the verification command.
- The provided personal wiki URL is used as a read-only target for `inspect` and one document metadata/read operation.

- [ ] **Step 1: Run all unit and package tests**

Run: `python -m pytest backend/tests skills/feishu-personal/test skills/feishu-personal/tests -q`

Expected: exit code 0 and no failed tests.

- [ ] **Step 2: Scan runtime code for fixed tenant references**

Run: `rg -n --glob '*.py' 'nio\.feishu\.cn' backend skills/feishu-personal`

Expected: no matches in runtime Python code. Matches in tests are reviewed individually; historical Markdown/JSON files are intentionally out of scope.

- [ ] **Step 3: Verify the session file exists without printing its contents**

Run: `if (Test-Path sessionss/feishu_web_session.json) { 'session file present' } else { 'session file missing' }`

Expected: `session file present`.

- [ ] **Step 4: Run read-only personal URL verification**

Run the existing skill runner with the session file available and the user-provided wiki URL for `inspect`, followed by `doc meta` or the existing read command. Capture only exit status, resolved host, object type, and title; redact session cookies and do not execute write commands.

Expected: the request reaches the personal tenant host, returns a successful Feishu response, and output URLs use the same personal tenant host.

- [ ] **Step 5: Commit final nested package changes and update outer gitlink**

Run from `skills/feishu-personal`: `git add lark_tools test tests; git commit -m "test: verify personal Feishu tenant compatibility"` only for changed nested files. Then from the workspace root run: `git add skills/feishu-personal docs/superpowers/plans/2026-08-17-feishu-tenant-host.md; git commit -m "fix: support personal Feishu tenant URLs"`.

Expected: nested and outer working trees are clean except for pre-existing ignored session data, and the outer repository records the new nested skill commit.
