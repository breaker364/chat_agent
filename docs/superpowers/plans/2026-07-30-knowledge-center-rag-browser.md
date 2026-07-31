# Knowledge Center RAG Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a usable knowledge-base browser with source-file visibility, document preview, chunk inspection, sync failure details, and direct PDF ingestion.

**Architecture:** Keep the existing `PersonalKnowledgeBase` as the source of truth. Extend it with source-file listing and document detail APIs, then replace the compact top knowledge panel with a two-pane Knowledge Center that still exposes import, sync, delete, and use-knowledge controls. Preserve deterministic local RAG behavior and avoid entity-specific hardcoding.

**Tech Stack:** FastAPI, Python stdlib plus optional `pypdf`, React, Vitest, Testing Library, existing CSS token system.

---

### Task 1: Backend Source Browser and PDF Support

**Files:**
- Modify: `backend/rag/service.py`
- Modify: `backend/main.py`
- Test: `backend/tests/test_rag_project_sync.py`

- [ ] **Step 1: Write failing backend tests**

Add tests that assert:
- `.pdf` is accepted by import/sync when PDF text extraction is available.
- `/knowledge/documents/{doc_id}` returns manifest plus chunk preview.
- `/knowledge/sources` returns pending/unsupported source files and indexed status.

- [ ] **Step 2: Run focused backend tests to verify RED**

Run: `C:/Users/hank.yu3/.conda/envs/env_311/python.exe -m pytest backend\tests\test_rag_project_sync.py -p no:cacheprovider`

Expected: failures for missing PDF support and missing endpoints.

- [ ] **Step 3: Implement minimal backend support**

Implement:
- `_SUPPORTED_SUFFIXES` includes `.pdf`.
- `_read_supported_file()` extracts PDF text with `pypdf.PdfReader`, falling back to a clear `RuntimeError` if dependency/text extraction fails.
- `list_source_files()` scans `knowledge_base/documents`.
- `get_document_detail()` returns manifest and chunks.
- FastAPI routes for `GET /knowledge/sources` and `GET /knowledge/documents/{doc_id}`.

- [ ] **Step 4: Run focused backend tests to verify GREEN**

Run: `C:/Users/hank.yu3/.conda/envs/env_311/python.exe -m pytest backend\tests\test_rag_project_sync.py -p no:cacheprovider`

Expected: all tests in the file pass.

### Task 2: Frontend Knowledge Center

**Files:**
- Modify: `frontend/src/App.jsx`
- Modify: `frontend/src/App.css`
- Test: `frontend/src/App.knowledgeBase.test.jsx`

- [ ] **Step 1: Write failing frontend tests**

Add tests that assert:
- The user can open Knowledge Center from the panel.
- Collections and documents render in a browser-style layout.
- Selecting a document fetches and displays preview/chunk/citation metadata.
- Sync failure details display file path and reason.

- [ ] **Step 2: Run focused frontend tests to verify RED**

Run: `npm test -- App.knowledgeBase.test.jsx --run`

Expected: failures for missing browser UI and missing detail rendering.

- [ ] **Step 3: Implement minimal frontend support**

Implement:
- Knowledge Center modal/overlay.
- Sidebar collection filter.
- Document table with status, chunk count, collection, and error details.
- Detail panel with source, chunk previews, and citation ids.
- Sync detail list using `payload.files`.

- [ ] **Step 4: Run focused frontend tests to verify GREEN**

Run: `npm test -- App.knowledgeBase.test.jsx --run`

Expected: focused frontend knowledge tests pass.

### Task 3: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run backend RAG test set**

Run: `C:/Users/hank.yu3/.conda/envs/env_311/python.exe -m pytest backend\tests\test_rag_core.py backend\tests\test_rag_project_sync.py backend\tests\test_rag_model_stack.py -p no:cacheprovider`

Expected: all selected backend RAG tests pass.

- [ ] **Step 2: Run frontend build/test verification**

Run: `npm test -- App.knowledgeBase.test.jsx --run`

Expected: focused frontend tests pass.

Run: `npm run build`

Expected: production build succeeds.
