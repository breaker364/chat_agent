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
    appendCommands: [],
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
      appendCommands: [],
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

export function getSubmitMode(runs, sessionId) {
  return isSessionRunning(runs, sessionId) ? "append" : "new_run";
}

export function getSubmitEndpoint(apiBase, runs, sessionId) {
  const base = apiBase || "";
  if (getSubmitMode(runs, sessionId) === "append") {
    return `${base}/sessions/${encodeURIComponent(sessionId || "default")}/runs/current/append`;
  }
  return `${base}/chat/stream`;
}

export function getSubmitButtonLabel(runs, sessionId) {
  return getSubmitMode(runs, sessionId) === "append" ? "追加到当前任务" : "发送";
}

export function recordAppendCommandEvent(runs, sessionId, event) {
  const appendId = event?.append_id || event?.appendId || "";
  return updateSessionRun(runs, sessionId, (current) => {
    const commands = [...(current.appendCommands || [])];
    const normalized = {
      append_id: appendId,
      run_id: event?.run_id || event?.runId || current.runId || "",
      status: event?.status || "",
      content: event?.content || "",
      sequence: event?.sequence,
      message: event?.message || "",
      updatedAt: Date.now(),
    };
    const index = appendId ? commands.findIndex((item) => item.append_id === appendId) : -1;
    if (index >= 0) {
      commands[index] = { ...commands[index], ...normalized };
    } else {
      commands.push(normalized);
    }
    return { appendCommands: commands };
  });
}

export function shouldClearComposerAfterSubmit({ ok }) {
  return Boolean(ok);
}
