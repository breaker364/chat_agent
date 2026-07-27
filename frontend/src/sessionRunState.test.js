import { describe, expect, it } from "vitest";

import {
  appendRunText,
  finishSessionRun,
  getSessionRun,
  isSessionRunning,
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
});
