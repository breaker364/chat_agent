## 1. Contracts and Red Tests

- [x] 1.1 Add red unit tests for `auto`, `required`, and `disabled` knowledge-policy resolution, legacy `knowledge_mode` compatibility, and contradictory request rejection.
- [x] 1.2 Add red unit tests for schema-valid and invalid research plans, including unknown source kinds, empty required routes, bounded query/scope fields, and no hard-coded entity-to-source behavior.
- [x] 1.3 Add red unit tests for research-trace serialization that prove chain-of-thought, raw evidence text, raw tool payloads, embeddings, cookies, authorization values, and provider secrets are excluded.
- [x] 1.4 Add red unit tests for source and route-transition budget consumption, atomic exhaustion handling, terminal state behavior, and injected clock/deadline handling.
- [x] 1.5 Define generic policy, plan, observation, assessment, budget, trace, and terminal-outcome models in a dedicated agentic-research module with strict validation and bounded fields.

## 2. Read-Only Evidence Adapters

- [x] 2.1 Add deterministic fake adapters and fixtures for personal knowledge, workspace files, web search/fetch, no-result, retryable error, access failure, conflicting evidence, and prompt-injection-like source text.
- [x] 2.2 Add red adapter tests proving `knowledge_search` results preserve bounded citations and retrieval settings, workspace evidence remains within the configured root, and web evidence preserves bounded URL/title/snippet fields.
- [x] 2.3 Add red safety tests proving import, sync, delete, login, write, and configuration tools cannot be selected or executed by the automatic evidence registry.
- [x] 2.4 Implement a generic source-kind registry and normalized read-only adapters over the existing knowledge, workspace, and web tool contracts without changing their standalone APIs.
- [x] 2.5 Implement source-output normalization, excerpt caps, citation extraction, error categorization, and untrusted-content handling before results reach the assessor or synthesis prompt.

## 3. Planner, Assessor, and Bounded Loop

- [x] 3.1 Add red planner tests for direct-answer, personal-knowledge, workspace, web, and mixed-source plans using injected structured-output fakes.
- [x] 3.2 Add red assessment tests for sufficient evidence, irrelevant/no-result evidence, retryable errors, stale/current needs, required-source insufficiency, and material source conflicts.
- [x] 3.3 Implement injectable planner and assessor protocols plus a production structured-model adapter that returns validated plan/assessment objects without persisting free-form reasoning.
- [x] 3.4 Implement `AgenticResearchOrchestrator` with plan, execute, assess, replan, evidence-gap, and answer-ready states; enforce runtime budgets before every evidence attempt.
- [x] 3.5 Add deterministic integration tests proving auto-policy fallback from insufficient knowledge to an allowed next source, query refinement within budget, no retrieval for direct requests, and no extra call after a terminal state.
- [x] 3.6 Add deterministic integration tests proving `required` does not silently fall back to public or workspace sources, `disabled` never invokes knowledge search, and conflicting evidence yields verification or a labeled conflict outcome.
- [x] 3.7 Integrate the coordinator with the existing LangGraph ReAct flow so evidence acquisition is explicit and bounded while normal task tools retain their current authority and behavior.
- [x] 3.8 Update synthesis context/prompt construction to admit only usable observations, distinguish source classes in mixed answers, and produce an explicit evidence limitation for gaps or unresolved conflicts.

## 4. Configuration and Service Contracts

- [x] 4.1 Add red configuration tests for `agentic_research.enabled`, source-call limits, route-transition limit, deadline, result/excerpt limits, and rejection of secret-bearing configuration fields.
- [x] 4.2 Implement configuration loading with conservative defaults, validation, and a disabled-mode path that leaves existing chat behavior unchanged.
- [x] 4.3 Add red API tests for streaming and non-streaming chat requests that accept `knowledge_policy`, preserve legacy `knowledge_mode`, reject conflicts before execution, and return bounded research summaries.
- [x] 4.4 Implement request parsing and response/event integration in `backend/main.py`, replacing force-use prompt suffixes with the resolved policy and sanitized evidence bundle.
- [x] 4.5 Add regression tests proving direct `knowledge_search`, workspace-file, and web-tool endpoints/contracts remain usable outside the coordinator.

## 5. Frontend and Provenance UX

- [x] 5.1 Add frontend red tests for an `auto`/`required`/`disabled` knowledge-policy control, default auto submission, legacy request compatibility, and validation-error display.
- [x] 5.2 Replace the binary knowledge-base switch with an accessible compact policy control and send `knowledge_policy` on chat requests.
- [x] 5.3 Add frontend tests for source-class labels, compact route outcome, budget/evidence-gap messaging, and mixed personal/workspace/web citations without exposing internal reasoning.
- [x] 5.4 Render bounded research provenance from chat responses and server-sent events while preserving existing citation rendering and session history behavior.

## 6. Safety, Observability, and Verification

- [x] 6.1 Add red tests proving untrusted retrieved text cannot alter policy, budgets, registered tools, or request authority, and that blocked source-tool requests have no side effects.
- [x] 6.2 Implement sanitized route metrics/logging and compact progress events using source categories, counters, terminal outcome, and duration only.
- [x] 6.3 Run focused unit and integration tests after each implementation group, including deterministic offline planner/assessor/adapters and API contract tests.
- [x] 6.4 Run existing agent, RAG core, BM25, retrieval-stack, Feishu-import, tool, session, and application endpoint regression suites.
- [x] 6.5 Run frontend unit tests and a browser-level chat flow for all three policies, direct-answer behavior, fallback, required evidence gap, and provenance display.
- [x] 6.6 Update user-facing RAG/agent documentation with policy semantics, source selection boundaries, provenance interpretation, privacy guarantees, configuration, and troubleshooting.
- [x] 6.7 Run Python compilation, `git diff --check`, OpenSpec strict validation, and a final review for hard-coded entity mappings, raw evidence leakage, credentials, embeddings, and chain-of-thought exposure.
