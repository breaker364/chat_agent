const FEEDBACK_STORAGE_KEY = "chat-agent:message-feedback";

export function readFeedbackStore() {
  try {
    const raw = window.localStorage.getItem(FEEDBACK_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function toggleFeedback(store, sessionId, messageIndex, rating) {
  const next = { ...store };
  const sessionEntries = { ...(next[sessionId] || {}) };
  if (sessionEntries[messageIndex] === rating) {
    delete sessionEntries[messageIndex];
  } else {
    sessionEntries[messageIndex] = rating;
  }
  next[sessionId] = sessionEntries;
  try {
    window.localStorage.setItem(FEEDBACK_STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Ignore localStorage failures.
  }
  return next;
}

export function reportFeedback(base, payload) {
  return fetch(`${base}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(payload),
  });
}
