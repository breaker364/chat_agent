## Why

The agent can call personal knowledge, workspace-file, and web tools, but today its choice is mostly prompt guidance and the UI can force a knowledge-base search. It does not explicitly plan which evidence source is appropriate, assess whether tool output answers the question, or deliberately change strategy when evidence is missing.

This change makes that decision-observation-replanning loop a first-class, testable capability. It turns the current tool-enabled RAG path into an agentic research workflow without treating every prompt as a knowledge-base question or weakening source provenance.

## What Changes

- Add an evidence-routing policy that lets the agent choose direct answer, personal knowledge, workspace files, web research, or a bounded combination based on the user's information need and freshness requirements.
- Add an explicit research loop: formulate a source plan, execute the selected tool, classify evidence sufficiency, refine or switch sources when warranted, and stop with a grounded answer or an explicit evidence gap.
- Replace the binary knowledge-mode behavior with an `auto`, `required`, and `disabled` policy so users can retain control while `auto` leaves source selection to the agent.
- Define a bounded, source-neutral evidence result and route trace containing source type, citations, outcome, rationale category, attempts, and budget usage. It MUST NOT expose raw documents, embeddings, cookies, tokens, or internal model reasoning.
- Preserve the existing hybrid RAG stack, local file tools, and web tools as independent evidence providers; no provider is selected by hard-coded entity, domain, or keyword mappings.
- Add configuration and tests for source budgets, fallback rules, conflict handling, user overrides, and offline deterministic routing/evidence scenarios.

## Capabilities

### New Capabilities

- `agentic-retrieval-orchestration`: Plans, executes, evaluates, and transparently reports bounded multi-source evidence retrieval for agent answers.

### Modified Capabilities

- None.

## Impact

- Affected backend areas: `backend/agent.py`, agent prompts/policy, tool registration and result handling, RAG tool contracts, request validation in `backend/main.py`, and runtime configuration.
- Affected frontend areas: chat request controls and rendered provenance/route status for automatic, required, and disabled knowledge use.
- Affected behavior: factual questions can be routed to the most suitable available source and can move to another source after insufficient evidence; creative, computational, and conversational requests can complete without unnecessary retrieval.
- Affected tests: deterministic agent orchestration, tool-selection and fallback contracts, API validation, budget/trace safety, RAG/web/local-file regressions, and browser interaction tests.
