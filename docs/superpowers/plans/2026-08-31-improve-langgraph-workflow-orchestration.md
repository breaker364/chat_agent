# LangGraph Workflow Orchestration Implementation Plan

> **For agentic workers:** Execute in this shared workspace using TDD: every production change follows a focused failing test, the expected failure is observed, then the smallest implementation is added and retested.

**Goal:** Add an opt-in, fixed-topology LangGraph workflow that coordinates typed tasks, constrained routing, Executor and Research subgraphs, checkpoint recovery, approval pauses, and compatible application events.

**Architecture:** New focused modules own state/reducers, policy validation, compatibility imports, executor execution, research execution, and the top-level workflow. The existing ReAct driver remains the default behind a validated feature flag; an adapter permits controlled rollout without replacing `SessionStore` as the durable business record.

**Tech Stack:** Python 3.12, Pydantic 2, LangChain Core, LangGraph, FastAPI, pytest/unittest.

## Global Constraints

- Graph topology is created only in application code and compiled before invocation; models can supply data but cannot change nodes or edges.
- Route output is schema-bounded, and deterministic policy validation makes the final decision.
- Tools are granted by declared capabilities and execution remains behind existing tool wrappers where they are supplied.
- Session facts remain in `SessionStore`; checkpoints only recover a specific `(session_id, run_id)` workflow run.
- New code must not use entity-specific matching or domain allowlists.
- The workflow flag defaults to disabled so the existing API remains the compatibility path until rollout is enabled.

---

### Task 1: State, runtime configuration, and policy contracts

**Files:**

- Create: `backend/workflow_compat.py`
- Create: `backend/workflow_state.py`
- Create: `backend/workflow_policy.py`
- Modify: `backend/config.py`
- Test: `backend/tests/test_workflow_orchestration.py`

**Interfaces:**

- Produces `initial_workflow_state(session_id, run_id, request) -> WorkflowState`.
- Produces reducer functions including `merge_task_results(current, update)`.
- Produces `RouteProposal`, `enforce_route(...)`, and `validate_plan(...)`.
- Produces `WorkflowConfig` through `load_workflow_config(...)`.

- [ ] **Step 1: Write failing contract tests**

```python
merged = merge_task_results(current, late)
assert merged["task-1"]["output"] == "new"
with pytest.raises(RoutePolicyError):
    enforce_route(proposal, request="read", capabilities={"read"})
```

- [ ] **Step 2: Verify the tests are red**

Run: `.\\.venv_py312\\Scripts\\python.exe -m pytest backend/tests/test_workflow_orchestration.py -q`

Expected: import failures for the workflow modules.

- [ ] **Step 3: Add minimal typed contracts and validation**

```python
def validate_plan(tasks: Sequence[Mapping[str, Any]], *, capabilities: set[str]) -> list[TaskSpec]:
    # Normalize, enforce IDs/dependencies/capabilities, then reject cycles.
    ...
```

- [ ] **Step 4: Verify focused tests are green**

Run: `.\\.venv_py312\\Scripts\\python.exe -m pytest backend/tests/test_workflow_orchestration.py -q`

Expected: state and policy contract tests pass.

### Task 2: Bounded Executor subgraph

**Files:**

- Create: `backend/executor_graph.py`
- Test: `backend/tests/test_workflow_orchestration.py`

**Interfaces:**

- Consumes a model and mapping of tools.
- Produces `build_executor_graph(model, tools, tool_metadata=None)` returning a compiled `StateGraph`.
- Returns a finite `TaskResult` shape and never mutates its parent plan.

- [ ] **Step 1: Write failing allowlist and success-path tests**

```python
result = asyncio.run(graph.ainvoke(task_state))
assert result["status"] == "failed"
assert forbidden.calls == []
```

- [ ] **Step 2: Verify executor tests are red**

Run: `.\\.venv_py312\\Scripts\\python.exe -m pytest backend/tests/test_workflow_orchestration.py::ExecutorSubgraphTests -q`

Expected: import failures until the Executor graph exists.

- [ ] **Step 3: Implement prepare, agent, tools, and validate nodes**

```python
graph.add_edge("executor_prepare", "executor_agent")
graph.add_conditional_edges("executor_agent", decide_executor_next)
graph.add_edge("executor_tools", "executor_agent")
```

- [ ] **Step 4: Verify executor tests are green**

Run: `.\\.venv_py312\\Scripts\\python.exe -m pytest backend/tests/test_workflow_orchestration.py::ExecutorSubgraphTests -q`

Expected: authorized tool completes; ungranted tool is rejected before invocation.

### Task 3: Research subgraph and evidence result boundary

**Files:**

- Create: `backend/research_graph.py`
- Modify: `backend/agentic_research/runtime.py`
- Test: `backend/tests/test_research_graph.py`

**Interfaces:**

- Consumes existing `AgenticResearchOrchestrator` or injected planner/assessor/registry.
- Produces `build_research_graph(runtime)` and a limited result containing summary, evidence references, status, gaps, budget, and audit events.

- [ ] **Step 1: Write failing tests for evidence deduplication and partial results**

```python
result = graph.invoke(research_state)
assert len(result["evidence"]) == 1
assert result["status"] == "partial"
```

- [ ] **Step 2: Verify research tests are red**

Run: `.\\.venv_py312\\Scripts\\python.exe -m pytest backend/tests/test_research_graph.py -q`

Expected: the new graph module cannot yet be imported.

- [ ] **Step 3: Implement fixed planning/collection/assessment/finalization nodes**

```python
graph.add_edge("research_prepare", "research_plan")
graph.add_edge("research_plan", "collect_evidence")
graph.add_conditional_edges("assess_evidence", decide_research_next)
```

- [ ] **Step 4: Verify research and existing coordinator tests are green**

Run: `.\\.venv_py312\\Scripts\\python.exe -m pytest backend/tests/test_research_graph.py backend/tests/test_agentic_research_orchestrator.py -q`

Expected: new result boundary is enforced without breaking current research orchestration.

### Task 4: Top-level workflow, fan-out, checkpoints, and approvals

**Files:**

- Create: `backend/workflow_graph.py`
- Test: `backend/tests/test_workflow_orchestration.py`

**Interfaces:**

- Produces `build_workflow_graph(route_model, planner, executor, research=None, capabilities=..., checkpointer=None)`.
- Uses `Send("dispatch_task", ...)` only for validated ready tasks.
- Uses a checkpointer keyed by an externally supplied stable workflow thread ID.

- [ ] **Step 1: Write failing top-level dispatch and topology tests**

```python
result = asyncio.run(graph.ainvoke(initial_state, config=config))
assert result["status"] == "completed"
assert set(result["task_results"]) == {"a", "b"}
```

- [ ] **Step 2: Verify workflow tests are red**

Run: `.\\.venv_py312\\Scripts\\python.exe -m pytest backend/tests/test_workflow_orchestration.py::TopLevelWorkflowTests -q`

Expected: workflow graph import failure until the factory exists.

- [ ] **Step 3: Implement fixed nodes and conditional routes**

```python
graph.add_conditional_edges("scheduler", schedule_ready_tasks)
graph.add_edge("dispatch_task", "join")
graph.add_conditional_edges("assess", decide_assessment)
```

- [ ] **Step 4: Implement approval pause and checkpoint recovery tests**

```python
result = graph.invoke(state, config={"configurable": {"thread_id": thread_id}})
assert "__interrupt__" in result
```

- [ ] **Step 5: Verify top-level workflow tests are green**

Run: `.\\.venv_py312\\Scripts\\python.exe -m pytest backend/tests/test_workflow_orchestration.py -q`

Expected: all top-level state, policy, executor, scheduling, approval, and recovery tests pass.

### Task 5: Runtime adapter, persistence, events, rollout, and regression suite

**Files:**

- Create: `backend/workflow_runtime.py`
- Modify: `backend/agent.py`
- Modify: `backend/main.py`
- Modify: `backend/session_store.py`
- Modify: `runtime_config.json`
- Modify: `docs/langgraph-state-orchestration.md`
- Test: `backend/tests/test_workflow_runtime.py`
- Test: `backend/tests/test_workflow_orchestration.py`

**Interfaces:**

- Produces an opt-in runtime selected only when `workflow.enabled` is true.
- Maps graph lifecycle events into the existing SSE event names with optional workflow metadata.
- Persists confirmed final workflow results through `SessionStore` without treating checkpoints as long-term session history.

- [ ] **Step 1: Write failing feature-flag and event-mapping tests**

```python
assert select_agent_runtime(config).kind == "legacy"
assert map_workflow_event(event)["event"] == "progress"
```

- [ ] **Step 2: Verify runtime tests are red**

Run: `.\\.venv_py312\\Scripts\\python.exe -m pytest backend/tests/test_workflow_runtime.py -q`

Expected: imports or expected behavior fail before the adapter exists.

- [ ] **Step 3: Implement disabled-by-default selection and compatibility adapter**

```python
if not workflow_config.enabled:
    return legacy_agent
return WorkflowRuntime(...)
```

- [ ] **Step 4: Add session writeback, approval resume handling, and SSE metadata mapping**

```python
store.record_stage_result(session_id, "workflow", payload)
```

- [ ] **Step 5: Verify targeted and full regression suites**

Run: `.\\.venv_py312\\Scripts\\python.exe -m pytest backend/tests -q`

Expected: workflow tests and the existing backend suite pass.

- [ ] **Step 6: Validate the OpenSpec change and update operational documentation**

Run: `openspec validate improve-langgraph-workflow-orchestration --type change --strict --no-interactive --json`

Expected: the proposal validates and the runtime documentation explains fixed topology, feature-flag rollout, recovery, approvals, and rollback.
