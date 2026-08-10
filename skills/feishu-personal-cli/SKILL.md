---
name: feishu-personal-cli
description: Standalone CLI-only Feishu/Lark personal-session operations for docx, wiki, sheets, bitable, whiteboard, chat, calendar, and minutes. Executes standardized `lark ...` commands in-process through the lark_tools package. Use when the agent must operate Feishu personal resources through explicit CLI commands only, without natural-language routing.
---

# Feishu Personal CLI

This is a self-contained CLI-only skill for Feishu/Lark personal-session operations.
It executes `lark ...` commands in-process through the locally bundled `lark_tools/` package.
No dependency on the `feishu-personal` skill.

## Contract

- Always call this skill with one explicit command beginning with `lark ` or `lark_cli `.
- Never pass a natural-language instruction as `request`.
- Translate the user's intent into a concrete CLI command before calling `use_skill`.
- Prefer the command reference below. Do not read bundled implementation files unless the CLI output is insufficient to debug a failure.
- If a command fails, inspect the returned CLI error and issue a corrected CLI command. Do not retry the same invalid command.

Correct:

```text
use_skill(skill_name="feishu-personal-cli", request="lark doc read <docx-url>")
```

Incorrect:

```text
use_skill(skill_name="feishu-personal-cli", request="read this Feishu document")
```

## Authentication

- Check login with `feishu_login_status` before using this skill when the task needs live Feishu access.
- If login is invalid, ask the user to refresh the Feishu web session instead of trying ad hoc HTTP calls.

## Document Commands

```text
lark doc read <token-or-url>
lark doc meta <token-or-url>
lark doc length <token-or-url>
lark doc download <token-or-url> [--out output.md]
lark doc blocks <token-or-url> [--type TYPE] [--grep PATTERN] [--limit N] [--offset N] [--format json|compact] [--preview N]
lark doc create <title> [--text "..."] [--md-file path] [--stdin] [--parent <wiki-token>]
lark doc append <token-or-url> [--text "..."] [--md-file path] [--stdin]
lark doc replace <token-or-url> --md-file path [--dry-run]
lark doc set-title <token-or-url> <new-title>
lark doc delete-block <token-or-url> <block-id>
lark doc edit <token-or-url> <block-id> --replace "new text"
lark doc edit-code <token-or-url> <block-id> [--language LANG] [--content TEXT | --content-file PATH]
lark doc insert-image <token-or-url> <image-path>
```

For long Markdown or generated content, write it to `tmp/` and use `--md-file` or `--stdin` rather than placing large text inline.

`doc replace` is a complete-content operation, not an append. It preflights the document version and writable root children, performs one version-checked mutation, and reads the document back to verify its canonical content hash and block summary. Use `--dry-run` to inspect the replacement plan without a remote write. Conflicts, empty sources, and verification mismatches are reported as blocked results; they never trigger a corrective append.

## Bitable Commands

```text
lark bitable create [title] [--tz Asia/Shanghai] [--parent <wiki-token>]
lark bitable tables <token-or-url>
lark bitable schema <token-or-url> [tableId]
lark bitable views <token-or-url> [tableId]
lark bitable records <token-or-url> [tableId] [--limit N] [--offset N] [--view ID|NAME] [--filter <field> <op> [value]]... [--sort field[:asc|desc]]... [--group field] [--all]
lark bitable download <token-or-url> [tableId] [--out path]
lark bitable add-table <token-or-url> <table-name>
lark bitable add-field <token-or-url> <tableId> <name> [--type text|number|checkbox|url|datetime] [--format 0.###]
lark bitable add-fields-batch <token-or-url> <tableId> <json-array|@path|--json-file path>
lark bitable add-record <token-or-url> <tableId> <field=value>...
lark bitable add-records-batch <token-or-url> <tableId> <json-array|@path|--json-file path>
lark bitable set-record <token-or-url> <tableId> <recordId> <field=value>...
lark bitable delete-record <token-or-url> <tableId> <recordId>
lark bitable delete-records <token-or-url> --table <tableId> --record-ids <id1,id2>
lark bitable rename-field <token-or-url> <tableId> <fieldId> <new-name>
lark bitable set-field-format <token-or-url> <tableId> <fieldId-or-name> <number-format>
```

Use batch commands for multiple fields or records. Put large JSON in `tmp/` and pass `@path` or `--json-file path`.

## Sheet Commands

```text
lark sheet tables <token-or-url>
lark sheet read <token-or-url> [--sheet NAME] [--all]
lark sheet download <token-or-url> [--sheet NAME] [--out path]
lark sheet images <token-or-url> [--sheet NAME] [--cell A1 | --cells A1,B2 | --range A1:B2] [--download] [--out path]
lark sheet set-cell <token-or-url> <range> <value> [--sheet NAME] [--style JSON] [--raw-string]
lark sheet set-formula <token-or-url> <cell> <formula> [--sheet NAME]
lark sheet set-range <token-or-url> <start-cell> <values-json> [--sheet NAME] [--style JSON] [--raw-string]
lark sheet set-style <token-or-url> <range> --style JSON [--sheet NAME]
lark sheet insert-row <token-or-url> --at N [--count N] [--sheet NAME]
lark sheet delete-row <token-or-url> --at N [--count N] [--sheet NAME]
lark sheet insert-col <token-or-url> --at A [--count N] [--sheet NAME]
lark sheet delete-col <token-or-url> --at A [--count N] [--sheet NAME]
lark sheet add-tab <token-or-url> --name NAME [--at N]
lark sheet delete-tab <token-or-url> --name NAME
lark sheet rename-tab <token-or-url> --from OLD --to NEW
```

## Whiteboard Commands

Whiteboards are blocks inside a docx/wiki document. To create a new whiteboard, first create or identify a docx/wiki container.

```text
lark whiteboard list <docx-or-wiki-url>
lark whiteboard meta <block-token-or-docx-url> [--block-id <block-token>]
lark whiteboard read <block-token-or-docx-url> [--block-id <block-token>] [--format raw|ai|json|code] [--index N] [-o path]
lark whiteboard download <block-token-or-docx-url> [--block-id <block-token>] [--out path]
lark whiteboard create <docx-or-wiki-url>
lark whiteboard plantuml <block-token-or-docx-url> [--block-id <block-token>] [--source <file|-> | --text "code"] [--overwrite] [--dry-run]
lark whiteboard mermaid <block-token-or-docx-url> [--block-id <block-token>] [--source <file|-> | --text "code"] [--overwrite] [--dry-run]
```

Recommended whiteboard workflow:

1. `lark doc create <title> --text "..."` if no docx/wiki container exists.
2. `lark whiteboard create <docx-url>` to get `block_token`.
3. Write Mermaid or PlantUML source to `tmp/diagram.mmd` or `tmp/diagram.puml`.
4. `lark whiteboard mermaid <block-token> --source tmp/diagram.mmd --overwrite`.
5. `lark whiteboard read <block-token> --format ai` or `lark whiteboard download <block-token> --out tmp/whiteboard.png` to verify.

## Other Useful Commands

```text
lark search <query> [--type contacts|messages|docs|groups|apps|smart] [--today] [--from UNIX] [--to UNIX] [--limit N]
lark chat today [--with-bot] [--compact|--verbose|--html]
lark chat messages <chatId> [--count N] [--before POS] [--compact|--verbose|--html]
lark msg <messageId>
lark minutes meta <token-or-url>
lark minutes transcript <token-or-url>
lark minutes summary <token-or-url>
lark minutes chapters <token-or-url>
lark calendar list
lark calendar events <calendarId>
```

## Failure Handling

- If the runner returns `success: false`, read `error`, `stderr`, and `stdout`, then issue a corrected CLI command.
- Missing `<token|url>` means the agent must first obtain or create the required resource, not switch to natural language.
- If `whiteboard create` is requested without a docx/wiki URL, create a doc first or ask for a target document.
- Avoid `lark <subcommand> --help` unless the command table above is insufficient.
