## Context

The current application exposes one LangGraph ReAct agent with the complete set
of workspace, web, knowledge-base, and skill tools. The model can choose a
tool, but personal knowledge use is directed only by prompt wording and the
chat request has a boolean `knowledge_mode` that appends a force-use message.
Tool results are handled as independent events; there is no typed research
state that records an intended source, evidence coverage, a reason to retry,
or a terminal evidence gap.

The project already has strong source implementations to reuse:

- `knowledge_search` returns hybrid BGE-M3/BM25 candidates, reranking, bounded
  citations, and backend status.
- `list_directory`, `get_file_info`, and `read_file` are workspace-scoped,
  read-only evidence tools.
- `web_search` and `web_fetch` provide public-source evidence with existing
  cache and call limits.

The required change is orchestration, not a new vector store or a replacement
for the existing ReAct task agent. It must remain generic: no entity, domain,
or keyword-to-source mapping is encoded in application code. It must not make
an evidence lookup mandatory for creative, computational, or conversational
requests, and it must not reveal internal chain-of-thought, raw document text,
credentials, embeddings, or unbounded tool payloads.

## Goals / Non-Goals

**Goals:**

- Add a bounded agentic research loop with explicit plan, execution,
  observation, assessment, and replanning states.
- Let an injected/model-backed planner choose among direct response, personal
  knowledge, workspace files, web research, and a bounded multi-source route.
- Provide user policies `auto`, `required`, and `disabled` for personal
  knowledge use while preserving the legacy boolean request field.
- Evaluate evidence by answer coverage, source applicability, recency needs,
  errors, and citation availability rather than a single retrieval score.
- Enforce source-call and route-transition budgets in runtime code, not only in
  prompts, and terminate with a grounded answer or an explicit evidence gap.
- Surface a small route/provenance summary that distinguishes personal,
  workspace, and web evidence.
- Make planners, clocks, and evidence executors injectable so tests are
  deterministic and never require network access, personal sessions, or model
  downloads.

**Non-Goals:**

- Replacing BGE-M3, Qdrant, FAISS, BM25, the reranker, or the current document
  import/synchronization pipeline.
- Giving the research loop write, import, delete, login, or configuration
  mutation authority.
- Building unrestricted autonomous browsing, background research, or arbitrary
  tool generation.
- Persisting model reasoning, raw external content, document bodies, or
  credentials in route traces, conversation history, or logs.
- Guaranteeing factual correctness when all permitted evidence sources are
  unavailable or insufficient.

## Decisions

### 1. Add a bounded evidence orchestration graph around the existing agent

The application SHALL add an `AgenticResearchOrchestrator` that manages only
evidence acquisition. The existing ReAct agent remains responsible for normal
task execution, drafting, and approved non-research tools. The orchestrator
runs before synthesis and can run again only through a bounded replan edge.

```text
request + user policy
        |
        v
  policy resolution
        |
        v
     plan route ---------------------+
        |                            |
        v                            |
 execute read-only evidence tools    |
        |                            |
        v                            |
 assess evidence -- sufficient --> synthesize/final answer
        |
        +-- insufficient, budget remains --> replan route
        |
        +-- unavailable/conflicting/budget exhausted --> bounded evidence gap
```

The orchestrator state is request-scoped and contains a `ResearchPolicy`,
`ResearchPlan`, `EvidenceObservation` list, `EvidenceAssessment`,
`ResearchBudget`, and a sanitized `ResearchTrace`. Only the policy, selected
source types, outcome categories, counters, and citation references can leave
the runtime. The plan's free-form rationale and model messages remain
ephemeral.

The source graph is bounded by configuration: maximum route transitions,
per-source call limits, fetched-result size, and wall-clock deadline. A state
transition always consumes a budget atomically before the next executor call.
The graph cannot loop after a terminal assessment.

Alternative considered: tell the existing ReAct model to use tools more
carefully through a system prompt. That preserves flexibility but does not
provide enforceable budgets, explicit evidence assessment, deterministic tests,
or an auditable termination condition. A single deterministic keyword router
was also rejected because it cannot understand new user intents and would
violate the project's generic entity-handling constraint.

### 2. Use typed plans and assessments, with the LLM behind an injectable boundary

The planner produces a schema-validated `ResearchPlan`, not free-form tool
instructions. Its fields include:

```text
requires_evidence: bool
source_sequence: [direct | personal_knowledge | workspace | web]
query_or_scope: bounded source query or workspace scope
freshness_need: none | stable | current
success_criteria: requested fact facets and required citation types
```

The assessment boundary receives normalized observations and returns one of
`answer_ready`, `refine_same_source`, `try_next_source`, `report_conflict`, or
`evidence_gap`. Its input includes source availability, retrieval result count,
citations, bounded excerpts, query coverage markers, and freshness metadata;
it does not receive raw vectors or secrets. The default production adapter can
use the configured chat model with structured output. A deterministic fake
planner and assessor are required for tests.

Execution does not trust a planner-provided tool name. A registry maps generic
source kinds to a fixed read-only adapter:

| Source kind | Permitted adapters | Evidence returned |
| --- | --- | --- |
| `personal_knowledge` | `knowledge_search` | citations, score/settings summary, bounded excerpts |
| `workspace` | `list_directory`, `get_file_info`, `read_file` | workspace paths, bounded excerpts, file metadata |
| `web` | `web_search`, `web_fetch` | URLs, titles, snippets, bounded page extracts |
| `direct` | no evidence tool | explicit no-retrieval outcome |

The registry is generic and configuration-driven. It contains no mappings from
specific entities, brands, locations, people, or domains to a source. Search
queries remain model-derived from the user request. Write-capable tools and
remote-import tools are deliberately excluded from this registry.

Alternative considered: expose all existing tools directly to the planner. It
would allow an apparently informational question to mutate workspace or remote
knowledge state. Restricting the evidence loop to audited read-only adapters
preserves autonomy within a safe authority boundary.

### 3. Resolve personal-knowledge policy before planning

The HTTP request and frontend use a string `knowledge_policy`:

| Policy | Behavior |
| --- | --- |
| `auto` | Planner decides whether personal knowledge is relevant and may choose another source or direct answer. |
| `required` | The first evidence action MUST be personal knowledge. Insufficient knowledge produces an evidence gap unless the user explicitly requests supplemental sources. |
| `disabled` | The executor MUST NOT invoke personal knowledge, but workspace and web routes remain available in `auto` research planning. |

For compatibility, a request containing only legacy `knowledge_mode=true` maps
to `required`; `false` or an omitted legacy field maps to `auto`. Supplying
both fields with contradictory values is a validation error. The final agent
prompt receives the resolved policy and the evidence bundle, not a coercive
text suffix that bypasses the routing state.

Alternative considered: retain the current boolean and interpret `false` as
"never use knowledge." That would make automatic routing impossible and would
break existing callers that omitted the control expecting the agent's normal
tool behavior.

### 4. Treat evidence sufficiency as a semantic, provenance-aware decision

The assessor SHALL not use a universal score threshold as the only success
criterion: BM25, dense, reranker, web ranking, and local-file signals are not
globally comparable. It evaluates whether the returned evidence covers the
requested factual facets, has citations suitable for the selected source, is
compatible with stated freshness needs, and contains no unresolved source
error.

The following normalized outcomes drive the next state:

| Outcome | Next state |
| --- | --- |
| Evidence covers the request | `answer_ready` |
| No results, irrelevant results, or a retryable tool failure | refine query or try the next planned source |
| Sources disagree on a material fact | request bounded verification or return a conflict statement with source labels |
| Required source lacks evidence, source access is denied, or budget is spent | `evidence_gap` |
| Request needs no external evidence | direct synthesis without a tool call |

The synthesis prompt must use only observations marked usable. It must label
claims by source class when sources are combined and must state uncertainty
when it reaches `evidence_gap` or `report_conflict`.

Alternative considered: automatically fall back from any RAG miss to web
search. This can violate a user's expectation that a required personal
knowledge answer remains private and can produce an authoritative-looking
answer from an unrelated public source.

### 5. Make traces useful without storing reasoning or sensitive evidence

Each request returns an optional compact `research` summary:

```json
{
  "policy": "auto",
  "outcome": "answer_ready",
  "sources_attempted": ["personal_knowledge", "web"],
  "attempts": {"personal_knowledge": 1, "workspace": 0, "web": 1},
  "budget": {"route_transitions_used": 2, "route_transitions_limit": 3},
  "citation_counts": {"personal_knowledge": 2, "workspace": 0, "web": 2}
}
```

The trace omits internal chain-of-thought, planner rationale text, raw tool
payloads, document body text, vector values, cookies, authorization values, and
provider-specific secrets. Server logs use the same bounded categories and
numeric counters. The frontend displays source labels and terminal outcome,
not hidden reasoning.

Tool outputs are treated as untrusted data. Prompt-injection-like instructions
inside documents or web pages cannot alter the orchestration policy, budgets,
or registered tool authority.

### 6. Preserve existing tool behavior while adding an explicit research contract

The coordinator normalizes source adapter responses into a common evidence
model before assessment. Existing standalone calls to `knowledge_search`,
workspace tools, and web tools retain their public contracts. Agent task flows
that use non-evidence tools remain on the existing ReAct path.

The chat endpoints add `knowledge_policy` and an opt-in bounded research trace
field while preserving request fields and server-sent events used by existing
clients. The frontend replaces its binary switch with a compact segmented
control whose default is `auto`; it retains a clear `required` override for
users who explicitly need only indexed personal evidence.

## Risks / Trade-offs

- **[Planner misroutes an ambiguous request]** -> Use user policy overrides,
  schema validation, bounded source transitions, and deterministic scenario
  tests. Record the selected source class for review without storing reasoning.
- **[Extra model call increases latency and cost]** -> Skip planning for
  deterministic local operations where the user explicitly specifies a source;
  otherwise use one bounded structured planner call and cache no sensitive
  evidence.
- **[Evidence loop grows context excessively]** -> Normalize and cap excerpts,
  retain citation references, and pass only the active evidence bundle to
  synthesis.
- **[Public fallback leaks or changes answer scope]** -> Do not supplement a
  `required` personal-knowledge route without explicit user permission; label
  all source classes in mixed answers.
- **[Conflicting sources create false confidence]** -> Route to bounded
  verification where budget permits; otherwise state the conflict rather than
  selecting an unsupported winner.
- **[Prompt injection in retrieved material]** -> Keep policy and executor
  authority in runtime state; treat retrieved text as evidence only.
- **[Tool/API migration disrupts clients]** -> Accept the legacy boolean,
  make new fields additive, and preserve direct tool contracts.

## Migration Plan

1. Add pure data models, deterministic planner/assessor fakes, and unit tests
   for policy resolution, state transitions, and trace sanitization.
2. Add normalized read-only adapters over the existing knowledge, workspace,
   and web tools. Verify no write/import/delete/auth tool can enter the
   research registry.
3. Build the bounded coordinator and connect it to a test-only agent path;
   verify source sequence, replanning, conflict, and budget behavior offline.
4. Add request parsing with backward-compatible `knowledge_mode` mapping, then
   expose the compact research summary in streaming and non-streaming results.
5. Enable `auto` policy in the frontend while keeping `required` and
   `disabled` controls. Run end-to-end browser tests for all policies.
6. Roll out behind an `agentic_research.enabled` configuration switch with
   conservative budgets. Observe bounded route metrics and failures before
   making it the default.
7. Roll back by disabling the coordinator; legacy request mapping and direct
   tools remain available, and no index or document data migration is needed.

## Open Questions

- Whether a mixed-source answer should require a user-visible confirmation when
  the initial request explicitly names a private collection but does not set
  `required`.
- Whether source traces belong only in completed chat payloads or also as
  dedicated server-sent progress events.
- Which deployment-specific default budgets meet latency expectations while
  retaining enough room for one meaningful replan.
