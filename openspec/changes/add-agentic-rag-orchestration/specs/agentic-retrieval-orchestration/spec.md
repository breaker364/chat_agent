## ADDED Requirements

### Requirement: Resolve a user-controlled personal-knowledge policy
The system SHALL accept a `knowledge_policy` value of `auto`, `required`, or
`disabled` for chat requests and SHALL resolve it before planning evidence
collection. The legacy `knowledge_mode=true` value SHALL resolve to `required`;
an omitted or `false` legacy value SHALL resolve to `auto`. Conflicting policy
fields SHALL return a structured validation error before agent execution.

#### Scenario: Automatic policy is selected
- **WHEN** a chat request omits both `knowledge_policy` and a true legacy `knowledge_mode`
- **THEN** the system SHALL resolve the policy to `auto` and SHALL permit the planner to choose direct, personal-knowledge, workspace, web, or bounded mixed evidence

#### Scenario: Personal knowledge is required
- **WHEN** a chat request sets `knowledge_policy` to `required`
- **THEN** the first evidence action SHALL be a personal-knowledge search and the system SHALL NOT silently substitute another source when that search is insufficient

#### Scenario: Personal knowledge is disabled
- **WHEN** a chat request sets `knowledge_policy` to `disabled`
- **THEN** the system SHALL NOT invoke `knowledge_search` for that request while retaining eligible workspace and web routes

#### Scenario: Conflicting request controls are rejected
- **WHEN** a request supplies `knowledge_mode=true` and `knowledge_policy=disabled`
- **THEN** the system SHALL return an `invalid_request` error without invoking an agent or evidence tool

### Requirement: Plan source selection through a generic bounded contract
The system SHALL create a schema-validated research plan before invoking an
evidence source when the resolved policy and request require research. A plan
SHALL represent whether evidence is needed, an ordered list of generic source
kinds, bounded query or scope data, stated freshness need, and answer coverage
criteria. Source selection SHALL not depend on hard-coded entity names,
domains, brands, people, locations, or keyword-to-source mappings.

#### Scenario: Request does not need external evidence
- **WHEN** the planner determines a creative, computational, or conversational request does not require external evidence
- **THEN** the plan SHALL select `direct`, the executor SHALL make no evidence-tool call, and the trace SHALL record a direct outcome

#### Scenario: Planner selects personal knowledge
- **WHEN** an automatic-policy request asks about indexed personal material
- **THEN** the plan SHALL be allowed to select `personal_knowledge` and supply a bounded query to the registered knowledge adapter

#### Scenario: Planner selects a fresh public source
- **WHEN** an automatic-policy request requires current public information
- **THEN** the plan SHALL be allowed to select `web` without requiring a personal-knowledge search first

#### Scenario: Invalid plan is contained
- **WHEN** a planner returns an unknown source kind, an empty required source sequence, or invalid bounded fields
- **THEN** the system SHALL reject the plan, SHALL not invoke an arbitrary tool, and SHALL return a bounded evidence-gap or planner-error outcome

### Requirement: Execute only registered read-only evidence adapters
The system SHALL map generic source kinds to registered read-only evidence
adapters and SHALL normalize their outputs before assessment. The personal
knowledge adapter SHALL use hybrid knowledge search; the workspace adapter
SHALL use workspace-scoped inspection/read tools; the web adapter SHALL use
web search/fetch tools. Write, delete, import, sync, login, and configuration
tools SHALL NOT be executable from the automatic evidence loop.

#### Scenario: Knowledge route uses the configured retrieval stack
- **WHEN** the selected source kind is `personal_knowledge`
- **THEN** the executor SHALL call `knowledge_search` and SHALL preserve its bounded citations and active retrieval settings in the normalized observation

#### Scenario: Workspace route remains scoped
- **WHEN** the selected source kind is `workspace`
- **THEN** the executor SHALL use only workspace-scoped read-only adapters and SHALL reject paths outside the configured workspace boundary

#### Scenario: Automatic research cannot mutate state
- **WHEN** a planner attempts to select an import, deletion, synchronization, login, or write-capable tool
- **THEN** the executor SHALL deny that action, SHALL not change persisted state, and SHALL record a bounded policy-violation outcome

### Requirement: Assess evidence and replan within enforced budgets
The system SHALL assess each normalized observation for source availability,
citation presence, requested-facet coverage, freshness compatibility, and
material conflicts. It SHALL use the assessment to answer, refine the current
source query, try the next allowed source, report a conflict, or return an
explicit evidence gap. Runtime code SHALL enforce configured source-call and
route-transition budgets before each attempt.

#### Scenario: Insufficient knowledge triggers an allowed next route
- **WHEN** an `auto` plan receives no relevant personal-knowledge citations and its next permitted source is web or workspace
- **THEN** the assessor SHALL request a bounded replan and the executor SHALL attempt only the next registered source while budget remains

#### Scenario: Required knowledge remains private in scope
- **WHEN** a required personal-knowledge search lacks sufficient evidence and the user did not explicitly permit supplemental sources
- **THEN** the system SHALL terminate with an evidence-gap outcome and SHALL not invoke web or workspace evidence automatically

#### Scenario: Budget exhaustion terminates the loop
- **WHEN** the next evidence attempt would exceed a configured source-call or route-transition budget
- **THEN** the system SHALL make no additional evidence-tool call and SHALL return a bounded evidence-gap outcome that reports the exhausted budget category

#### Scenario: Conflicting evidence is not silently flattened
- **WHEN** usable evidence sources disagree on a material answer facet
- **THEN** the system SHALL either obtain bounded additional verification within budget or return a conflict outcome that identifies the source classes and uncertainty

### Requirement: Synthesize answers from usable evidence with source provenance
The system SHALL pass only observations marked usable by the assessment into
answer synthesis. It SHALL distinguish personal-knowledge, workspace, and web
citations in a mixed-source answer and SHALL state an evidence limitation when
the terminal outcome is an evidence gap or unresolved conflict.

#### Scenario: Mixed-source response labels provenance
- **WHEN** a final answer uses both personal-knowledge and web observations
- **THEN** the returned citations SHALL retain their source class and SHALL not represent public web evidence as personal knowledge or vice versa

#### Scenario: Evidence gap prevents unsupported factual completion
- **WHEN** all permitted source attempts are unavailable, irrelevant, or insufficient
- **THEN** the final response SHALL state that the available evidence is insufficient and SHALL not fabricate a source-grounded factual claim

### Requirement: Expose bounded safe research observability
The system SHALL expose a bounded research summary for a completed request,
including resolved policy, terminal outcome, source kinds attempted, per-source
attempt counts, budget counters, and citation counts. It SHALL NOT expose
model chain-of-thought, free-form planner rationale, raw document bodies, raw
tool payloads, embeddings, session cookies, authorization values, or provider
secrets in API responses, events, or logs.

#### Scenario: Successful research returns a compact trace
- **WHEN** an automatic research route completes with usable evidence
- **THEN** the response SHALL include a compact research summary with source kinds, outcome, attempt counts, budget usage, and citation counts

#### Scenario: Sensitive evidence is excluded from observability
- **WHEN** an evidence adapter receives document text or protected credentials
- **THEN** the research trace and logs SHALL exclude those values while retaining only bounded source and outcome metadata

### Requirement: Preserve deterministic and legacy behavior
The orchestration components SHALL accept injected planners, assessors,
executors, clocks, and configuration for deterministic tests. Existing direct
knowledge, workspace, and web tool contracts SHALL remain callable without the
agentic coordinator, and a disabled coordinator SHALL preserve legacy chat
behavior.

#### Scenario: Offline deterministic orchestration test
- **WHEN** a test injects fake planning and evidence adapters
- **THEN** it SHALL be able to verify route selection, fallback, and termination without network access, model downloads, or a personal session

#### Scenario: Coordinator is disabled
- **WHEN** `agentic_research.enabled` is false
- **THEN** existing chat requests and direct evidence tools SHALL retain their pre-change behavior and SHALL not emit an agentic research trace
