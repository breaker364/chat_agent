# Safe Document Replacement P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a typed, version-checked `lark doc replace` workflow that safely replaces structured document content, verifies the result, and keeps append/edit semantics distinct.

**Architecture:** Keep command normalization provider-agnostic at the backend skill boundary and expose the same normalized fields from the CLI compatibility adapter. Build replacement plans from parsed Markdown and a generic structural intermediate representation, then issue one root-child mutation guarded by the preflight version. Verify the canonical read-back hash and block summary; conflicts, empty sources, low-confidence plans, and mismatches fail without an append fallback.

**Tech Stack:** Python 3.10+, dataclasses, standard-library hashing/JSON, existing Feishu HTTP helpers, existing Markdown-to-block OT builder, pytest/unittest.

## Global Constraints

- Production logic MUST not branch on concrete entity names, domains, brands, people, places, or products; tests may use concrete command examples.
- Replacement MUST be distinct from append and block edit, require preflight/version checking, and never correct a failure by appending.
- Empty replacement sources are rejected unless a separately named clear operation is introduced; this change does not introduce that operation.
- Low-confidence structural imports require preview or explicit confirmation before remote mutation.
- Preserve existing uncommitted P0 changes and keep the CLI compatibility path usable.

## Task 1: Typed command manifests and guard integration

**Files:**
- Create: `backend/mutation_manifest.py` — normalized manifest model, parser, and mapping adapter.
- Modify: `backend/mutation_guard.py` — accept a manifest as the authoritative policy input while retaining string compatibility.
- Modify: `backend/skills.py` — normalize/validate manifests before dispatch and include the manifest in runner parameters/results.
- Create: `skills/feishu-personal-cli/lark_tools/command_manifest.py` — CLI command parser and manifest serializer.
- Modify: `skills/feishu-personal-cli/lark_tools/cli.py` — add `doc replace` and attach manifest/verification fields to document outcomes.
- Create: `backend/tests/test_mutation_manifest.py` and `skills/feishu-personal-cli/tests/test_command_manifest.py`.

- [ ] Write red parser tests for read, append, edit, replace, create, and delete; assert provider, resource kind, target, operation, mutability, canonical arguments, idempotency input, and verification mode.
- [ ] Run the focused parser tests and observe missing model/parser failures.
- [ ] Implement the dataclass and CLI/backend adapters with generic operation tables; reject unnormalizable mutating commands.
- [ ] Run focused parser and existing mutation-guard tests until green.
- [ ] Add the manifest to the skill result contract and make the guard consume it before dispatch; test generic wrapper routing, idempotency reuse, and append-versus-replace policy.

## Task 2: Safe replacement workflow

**Files:**
- Create: `skills/feishu-personal-cli/lark_tools/document_replacement.py` — source hashing, block summaries, preflight, root replacement map, and read-back verification.
- Modify: `skills/feishu-personal-cli/lark_tools/commands/doc_write.py` — expose `replace_markdown`/`cmd_doc_replace` using one version-checked mutation.
- Modify: `skills/feishu-personal-cli/lark_tools/commands/doc.py` — expose canonical block/content summary helpers.
- Modify: `skills/feishu-personal-cli/lark_tools/cli.py` — dispatch `doc replace <target> --md-file <path>` and dry-run/confirmation flags.
- Create: `skills/feishu-personal-cli/tests/test_document_replacement.py`.

- [ ] Write red tests for empty, paragraph, and mixed heading/list replacement, asserting old writable children are deleted and new blocks are inserted in one change map.
- [ ] Run those tests and observe the missing replacement API failure.
- [ ] Implement preflight (target version, writable child inventory, source hash, planned block summary) and a deletion-plus-insertion root mutation that passes the captured version to the API with conflict retries disabled.
- [ ] Run the three replacement tests green and assert `append_markdown` is never called.
- [ ] Add red conflict, empty-source, and read-back mismatch tests; assert structured blocked results and zero corrective appends.
- [ ] Implement version-advance, canonical hash, type-count verification plus stable result references and rollback/version-history metadata; run focused replacement tests green.

## Task 3: Generic structural import

**Files:**
- Create: `skills/feishu-personal-cli/lark_tools/structured_import.py` — typed node model, canonical serialization, confidence/diagnostic handling, and Markdown renderer.
- Create: `skills/feishu-personal-cli/tests/fixtures/structured_import_cases.json` — multi-page, headings, lists, tables, metadata, and ambiguous-order fixtures.
- Create: `skills/feishu-personal-cli/tests/test_structured_import.py`.

- [ ] Write red tests for title, heading, paragraph, ordered/unordered list, quote, code, table, and metadata nodes, stable canonical hashes, page metadata separation, and ambiguous reading order.
- [ ] Run tests and observe missing node/renderer APIs.
- [ ] Implement generic dataclass nodes and deterministic serialization with no content-specific cleanup maps.
- [ ] Implement extraction adapters for page/block records, rendering high-confidence nodes to replacement-ready Markdown/block summaries, and preview-required plans below the configured confidence threshold.
- [ ] Run structural tests green and verify changing source terms does not alter structural treatment.

## Task 4: Repair/append integration and rollout

**Files:**
- Modify: `backend/skills.py` or the relevant request-routing helper — route repair/overwrite/full-reformat manifests to `replace`, explicit add-content requests to `append`.
- Modify: `skills/feishu-personal-cli/skill_runner.py` — return normalized manifest and structured CLI result fields without losing human-readable output.
- Create: `backend/tests/test_document_mutation_integration.py`.
- Modify: `skills/feishu-personal-cli/SKILL.md` — document replace, dry-run, preview, and verification contract.

- [ ] Write red end-to-end tests for repair-to-replace and explicit-add-to-append routing, plus version-history rollback reference fields.
- [ ] Run tests and observe missing routing/result fields.
- [ ] Implement routing, runner result normalization, dry-run gating, and rollback reference reporting behind the P0 guard.
- [ ] Run focused integration tests, then the complete backend suite; document any frontend test limitation caused by locked dependencies.

## Self-review checklist

- Every OpenSpec task 1.1–4.3 is mapped to a concrete file and test cycle above.
- No step relies on a placeholder or an undefined interface; the replacement API is `replace_markdown(cookies, docx_token, markdown, *, dry_run=False, confirm=False)` and returns a structured dictionary.
- The manifest fields used by the backend (`provider`, `resource`, `target`, `operation`, `mutating`, `arguments`, `idempotency_input`, `verification_mode`) match the CLI serializer exactly.
- All remote writes pass through the version-checked replacement path or the existing explicit append/edit paths; no append fallback is introduced.
