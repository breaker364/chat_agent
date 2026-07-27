import { describe, expect, it } from "vitest";

import {
  appendRunText,
  applyRunAttribution,
  finishSessionRun,
  getAppendRequestPayload,
  getSubmitButtonLabel,
  getSubmitEndpoint,
  getSubmitMode,
  getSessionRun,
  isSessionRunning,
  recordAppendCommandEvent,
  shouldClearComposerAfterSubmit,
  pushRunEvent,
  startSessionRun,
} from "./sessionRunState";

describe("session run state", () => {
  it("keeps run updates scoped to the owning session", () => {
    let runs = startSessionRun({}, "session-a", "run-a");
    runs = startSessionRun(runs, "session-b", "run-b");

    runs = appendRunText(runs, "session-a", "hello");
    runs = pushRunEvent(runs, "session-a", "toolEvents", { type: "progress", message: "working" });

    expect(getSessionRun(runs, "session-a").streamingText).toBe("hello");
    expect(getSessionRun(runs, "session-a").toolEvents).toHaveLength(1);
    expect(getSessionRun(runs, "session-b").streamingText).toBe("");
    expect(getSessionRun(runs, "session-b").toolEvents).toEqual([]);
  });

  it("disables send only for the session that is currently running", () => {
    let runs = startSessionRun({}, "session-a", "run-a");

    expect(isSessionRunning(runs, "session-a")).toBe(true);
    expect(isSessionRunning(runs, "session-b")).toBe(false);

    runs = finishSessionRun(runs, "session-a", "completed");

    expect(isSessionRunning(runs, "session-a")).toBe(false);
  });

  it("stops only the targeted running session", () => {
    let runs = startSessionRun({}, "session-a", "run-a");
    runs = startSessionRun(runs, "session-b", "run-b");

    runs = finishSessionRun(runs, "session-a", "stopped");

    expect(getSessionRun(runs, "session-a").status).toBe("stopped");
    expect(isSessionRunning(runs, "session-a")).toBe(false);
    expect(isSessionRunning(runs, "session-b")).toBe(true);
  });

  it("routes running-session submit to append endpoint", () => {
    const runs = startSessionRun({}, "session-a", "run-a");

    expect(getSubmitMode(runs, "session-a")).toBe("append");
    expect(getSubmitEndpoint("/api", runs, "session-a")).toBe(
      "/api/sessions/session-a/runs/current/append"
    );
    expect(getSubmitButtonLabel(runs, "session-a")).toBe("追加到当前任务");
  });

  it("routes idle-session submit to normal chat stream", () => {
    const runs = startSessionRun({}, "session-a", "run-a");

    expect(getSubmitMode(runs, "session-b")).toBe("new_run");
    expect(getSubmitEndpoint("/api", runs, "session-b")).toBe("/api/chat/stream");
    expect(getSubmitButtonLabel(runs, "session-b")).toBe("发送");
  });

  it("records append status only on the originating session", () => {
    let runs = startSessionRun({}, "session-a", "run-a");
    runs = startSessionRun(runs, "session-b", "run-b");

    runs = recordAppendCommandEvent(runs, "session-a", {
      append_id: "append-1",
      run_id: "run-a",
      status: "queued",
      content: "追加内容",
    });

    expect(getSessionRun(runs, "session-a").appendCommands).toHaveLength(1);
    expect(getSessionRun(runs, "session-a").appendCommands[0].status).toBe("queued");
    expect(getSessionRun(runs, "session-b").appendCommands).toEqual([]);
  });

  it("keeps append text recoverable when append submit fails", () => {
    expect(shouldClearComposerAfterSubmit({ mode: "append", ok: false })).toBe(false);
    expect(shouldClearComposerAfterSubmit({ mode: "append", ok: true })).toBe(true);
    expect(shouldClearComposerAfterSubmit({ mode: "new_run", ok: false })).toBe(false);
    expect(shouldClearComposerAfterSubmit({ mode: "new_run", ok: true })).toBe(true);
  });

  it("updates the active run id from server SSE attribution", () => {
    let runs = startSessionRun({}, "session-a", "client-run-a");
    runs = startSessionRun(runs, "session-b", "client-run-b");

    runs = applyRunAttribution(runs, "session-a", {
      session_id: "session-a",
      run_id: "session-a:server-run-a",
    });

    expect(getSessionRun(runs, "session-a").runId).toBe("session-a:server-run-a");
    expect(getSessionRun(runs, "session-a").runIdSource).toBe("server");
    expect(getSessionRun(runs, "session-b").runId).toBe("client-run-b");
    expect(getSessionRun(runs, "session-b").runIdSource).toBe("client");
  });

  it("does not send a client-only run id in append payload", () => {
    let runs = startSessionRun({}, "session-a", "client-run-a");

    expect(getAppendRequestPayload(runs, "session-a", "追加内容")).toEqual({
      content: "追加内容",
    });

    runs = applyRunAttribution(runs, "session-a", {
      session_id: "session-a",
      run_id: "session-a:server-run-a",
    });

    expect(getAppendRequestPayload(runs, "session-a", "追加内容")).toEqual({
      content: "追加内容",
      run_id: "session-a:server-run-a",
    });
  });
});
