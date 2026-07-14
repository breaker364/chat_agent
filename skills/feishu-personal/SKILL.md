---
name: feishu-personal
description: Read and operate Feishu/Lark personal-session resources such as docx, wiki, sheets, bitable, whiteboard, chat, and calendar using a logged-in session cookie. Includes reading doc/wiki content and operating bitable tables, records, fields, and creating new data tables inside an existing base. Use when the user wants to access Feishu personal content that standard web fetching cannot parse reliably.
version: 1.0.0
author: Imported from Claude bundled skill
category: productivity
---

# Feishu Personal

Use the bundled `lark_tools` code in this skill directory to access Feishu/Lark personal-session content.

## Agent entrypoint

In this Chat Agent, `skill_runner.py` is the executable entrypoint for the `feishu-personal` skill.
It supports **two calling styles**:

### Style 1: Explicit CLI commands (preferred)

```text
lark <command> [args]
```

### Style 2: Natural language (auto-routed)

You can describe the operation in natural language.  The skill runner detects the
intent and translates it to the correct CLI command automatically.

| Intent | Auto-routed to |
|--------|---------------|
| "创建一个多维表格" / "create a base" | `lark bitable create <title>` |
| "列出这个base的表" / "list tables" | `lark bitable tables <url>` |
| "读取记录" / "read records" | `lark bitable records <url> --all` |
| "添加一条记录" / "add a record" | `lark bitable add-record <url> <tableId> <field=value>...` |
| "批量写入" / "batch write" | `lark bitable add-records-batch <url> <tableId> <json>` |
| "添加字段" / "add a field" | `lark bitable add-field <url> <tableId> <name> --type <type> [--format <number_format>]` |

### Complete bitable command reference

```text
lark bitable create <title>                        — create a new base
lark bitable tables <url-or-token>                  — list all tables
lark bitable schema <url> [tableId]                 — show field schema
lark bitable records <url> --table <tbl> --all      — read all records
lark bitable add-field <url> <tableId> <name> [--type text|number|checkbox|url|datetime] [--format 0.###]
lark bitable add-record <url> <tableId> <field=value>...
lark bitable add-records-batch <url> <tableId> <json_array>
lark bitable add-records-batch <url> <tableId> --json-file <path>
lark bitable add-records-batch <url> <tableId> @<path>
lark bitable set-record <url> <tableId> <recordId> <field=value>...
lark bitable delete-record <url> <tableId> <recordId>
lark bitable delete-record <url> --table <tableId> --record-id <recordId>
lark bitable delete-records <url> --table <tableId> --record-ids <id1,id2>
lark bitable set-field-format <url> <tableId> <fieldId|fieldName> <number_format>
lark bitable rename-field <url> <tableId> <fieldId> <new_name>
lark bitable download <url> [tableId] [--out path]
```

### Standard write workflow (avoids CSRF issues)

```
1. lark bitable create <title>                     → get obj_token + url
2. lark bitable tables <url>                        → get table_id
3. lark bitable add-field <url> <tableId> <name>    → repeat for each field, get field IDs; use `--format 0.###` or similar for precise number display
4. lark bitable add-records-batch <url> <tableId> '[{"fldXXX":"val1","fldYYY":"val2"}, ...]'
5. lark bitable records <url> --table <tbl> --all   → verify
```

## When to use

- The target is a Feishu/Lark personal resource such as `/docx/`, `/wiki/`, `/sheets/`, `/base/`, whiteboard, chat, or minutes.
- Standard `web_fetch` can reach the page but cannot reliably extract the structured content.
- The user has already logged in through the local Feishu web login flow and a reusable `session` cookie exists.

## Required workflow

1. Confirm `sessionss/feishu_web_session.json` contains a valid `session` cookie.
2. Reuse that cookie instead of asking the user to log in again.
3. Prefer the code under `lark_tools/commands/` for parsing structured resources and write operations:
   - `lark_tools.commands.doc` for docx/wiki
   - `lark_tools.commands.sheet` for sheets (including image extraction from cells)
   - `lark_tools.commands.img` for downloading images (chat and sheet images)
   - `lark_tools.commands.bitable` for base/bitable
   - `lark_tools.commands.bitable_write` for base/bitable mutations such as add table, add field, add record, and rename field
   - `lark_tools.commands.whiteboard` for whiteboards
4. For full CLI access, call this skill with an explicit command beginning with `lark ...`.
5. If the environment has broken `HTTP_PROXY` / `HTTPS_PROXY`, run requests with proxy inheritance disabled.
6. Return the extracted content or the specific subsection requested by the user.

### Sheet image download

- To extract images from spreadsheet cells, use `sheet images`:
  ```text
  lark sheet images <url>                      # list all images in the first sheet
  lark sheet images <url> --sheet "SheetName"   # specify a sheet
  lark sheet images <url> --cell E3             # filter to a specific cell
  lark sheet images <url> --cells E3,E4,E5      # filter to specific cells
  lark sheet images <url> --range E3:E7         # filter to a rectangular range
  lark sheet images <url> --cell E3 --download  # download the image(s)
  lark sheet images <url> --range E3:E7 --download --out ./img # download target range
  lark sheet images <url> --download --out ./img # download all to directory
  ```
- From Python:
  ```python
  from lark_tools.commands.sheet import fetch_sheet_images
  from lark_tools.commands.img import download_sheet_image
  # List images
  data = fetch_sheet_images(cookies, spreadsheet_token, sheet_id)
  for img in data['images']:
      print(f"Token={img['token']}, Cell={data['cell_map'].get(img['index']+1)}")
  # Download a specific image
  download_sheet_image(cookies, image_token, spreadsheet_token, 'output.png')
  ```
- The download endpoint is:
  `https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/v2/cover/{token}/?height=4096&mount_node_token={spreadsheet_token}&mount_point=sheet_image&policy=equal&width=4096`
- Images in sheets have cell-meta type `f1=7` (attachment type). The `f2` field is a 1-based index into the `f12.f3` protobuf resource list. Each image entry in `f12.f3` has `f1`=token, `f2`=dimensions.

### Whiteboard reading

- For Feishu/Lark whiteboard URLs containing `blockToken=...`, prefer:
  `whiteboard read <url-or-blockToken> --format ai`
- `--format ai` calls the web API:
  `/space/api/whiteboard/block?blockToken=<token>&reqVersion=1&clientVersion=12.4`
  and returns AI-friendly JSON with:
  - `whiteboard.meta`
  - hierarchical `nodes`
  - searchable `flat_nodes`
  - original `raw_nodes`
- Use `--format raw` only when the caller needs the exact Feishu node payload.
- Use `--format code` only for whiteboards generated from PlantUML/Mermaid syntax containers.
- If the input is a docx/wiki URL without `blockToken`, first list whiteboards with
  `whiteboard list <docx-or-wiki-url>`, then read the desired token with `--format ai`.

## Important notes

- This skill is code-first, not prompt-only.
- The imported code may use `requests` directly and inherit environment proxies; if access fails with proxy errors, patch the runtime call path to use a `requests.Session()` with `trust_env = False`.
- For docx reading, `lark_tools.commands.doc.cmd_doc_read(...)` is the preferred path because it reconstructs structured markdown from Feishu block data instead of scraping raw HTML.
- For whiteboard reading, `lark_tools.commands.whiteboard.cmd_whiteboard_read(..., fmt="ai")` is the preferred path because it normalizes Feishu canvas nodes into AI-friendly JSON.
- For adding a new data table inside an existing Base, use the `AddTableV2` reverse-engineered path through `lark_tools.bitable_base_ot` / `lark_tools.commands.bitable_write`.
- For downloading images from spreadsheet cells, use `fetch_sheet_images` to discover image tokens and `download_sheet_image` to download them. The static-resource endpoint used for chat images does NOT work for sheet images.
- For any lark CLI command not covered by the shortcut routes, use the explicit passthrough form `lark <command> [args]`; `skill_runner.py` invokes `lark_tools.cli.main()` in-process and bridges the agent's `sessionss/feishu_web_session.json` cookie into the CLI.

## Useful local paths

- `scripts/lark_cli.py`
- `lark_tools/cli.py`
- `lark_tools/auth.py`
- `lark_tools/commands/doc.py`
- `lark_tools/commands/sheet.py`
- `lark_tools/commands/img.py`
- `lark_tools/commands/bitable.py`
- `lark_tools/commands/bitable_write.py`
- `lark_tools/bitable_base_ot.py`
- `lark_tools/commands/whiteboard.py`

## Expected output

- Give the user the extracted content directly.
- If only part of a document is needed, return only that part.
- If parsing fails, explain whether the failure is from auth, proxy, or parser structure.
