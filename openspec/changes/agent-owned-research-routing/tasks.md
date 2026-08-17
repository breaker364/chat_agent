## 1. Regression Tests

- [x] 1.1 Verify registered knowledge, workspace, and web evidence tools remain visible to the main Agent.
- [x] 1.2 Verify plan_research_route validates plans, classifies failures, and never calls evidence services.
- [x] 1.3 Verify direct answers, automatic evidence requests, required knowledge policy, explicit web search, disabled knowledge, and source budgets.
- [x] 1.4 Verify answer-time file and Python access to the configured internal knowledge index is denied.
- [x] 1.5 Verify planner, evidence calls, and bounded route metadata are persisted without reasoning, raw evidence, or credentials.

## 2. Agent-Owned Routing

- [x] 2.1 Expose registered read-only evidence tools to the main ReAct Agent and remove hidden coordinator execution.
- [x] 2.2 Register the read-only plan_research_route tool around ModelResearchPlanner with schema validation and bounded errors.
- [x] 2.3 Enforce request-level source policy and source-call budgets in the evidence tool wrapper.
- [x] 2.4 Move streaming and synchronous chat routing into the Agent path and preserve correlated error handling.
- [x] 2.5 Update prompts for direct answers, explicit source priority, planner use for ambiguous evidence requests, citations, and index protection.

## 3. Boundaries and Observability

- [x] 3.1 Protect the configured knowledge index from answer-time file and Python tools without entity-specific hardcoding.
- [x] 3.2 Persist bounded research_route metadata and expose compatible SSE and synchronous response metadata.
- [x] 3.3 Display direct, explicit, planned, and failed route states in the frontend without internal reasoning.

## 4. Verification

- [x] 4.1 Run deterministic fake tests for Agent routing, RAG tools, tool history, and file access.
- [x] 4.2 Run the relevant backend test suites and record environment limitations.
- [x] 4.3 Manually verify direct, explicit knowledge, explicit web, and planner failure routes through the chat API.
