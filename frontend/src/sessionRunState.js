export function getSessionRun(runs, sessionId) {
  return (runs && runs[sessionId]) || {
    runId: "",
    status: "idle",
    streamingText: "",
    toolEvents: [],
    activityItems: [],
    debugEvents: [],
    usage: null,
    error: "",
  };
}

export function startSessionRun(runs, sessionId, runId) {
  return {
    ...(runs || {}),
    [sessionId]: {
      runId: runId || "",
      status: "running",
      streamingText: "",
      toolEvents: [],
      activityItems: [],
      debugEvents: [],
      usage: null,
      error: "",
      startedAt: Date.now(),
      updatedAt: Date.now(),
    },
  };
}

export function updateSessionRun(runs, sessionId, updater) {
  const current = getSessionRun(runs, sessionId);
  return {
    ...(runs || {}),
    [sessionId]: {
      ...current,
      ...updater(current),
      updatedAt: Date.now(),
    },
  };
}

export function appendRunText(runs, sessionId, text) {
  return updateSessionRun(runs, sessionId, (current) => ({
    streamingText: `${current.streamingText || ""}${text || ""}`,
  }));
}

export function setRunText(runs, sessionId, text) {
  return updateSessionRun(runs, sessionId, () => ({
    streamingText: String(text || ""),
  }));
}

export function pushRunEvent(runs, sessionId, key, event) {
  return updateSessionRun(runs, sessionId, (current) => ({
    [key]: [...(current[key] || []), event],
  }));
}

export function replaceRunEvents(runs, sessionId, key, events) {
  return updateSessionRun(runs, sessionId, () => ({
    [key]: [...(events || [])],
  }));
}

export function finishSessionRun(runs, sessionId, status = "completed", extra = {}) {
  return updateSessionRun(runs, sessionId, () => ({
    ...extra,
    status,
  }));
}

export function clearSessionRun(runs, sessionId) {
  const next = { ...(runs || {}) };
  delete next[sessionId];
  return next;
}

export function isSessionRunning(runs, sessionId) {
  return getSessionRun(runs, sessionId).status === "running";
}
