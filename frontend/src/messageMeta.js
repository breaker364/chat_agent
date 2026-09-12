export const ATTACHMENT_MESSAGE_MARKER = "Attached files available in the workspace:";
export const ERROR_MESSAGE_PREFIX_EN = "Request failed: ";
export const ERROR_MESSAGE_PREFIX_ZH = "请求失败: ";

function parseJsonLike(value) {
  if (value && typeof value === "object") return value;
  if (typeof value !== "string") return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

export function toolResultIsError(content) {
  if (content && typeof content === "object") {
    return Boolean(content.error) || content.ok === false;
  }
  const parsed = parseJsonLike(content);
  if (parsed) {
    return Boolean(parsed.error) || parsed.ok === false;
  }
  if (typeof content === "string") {
    return /\b(failed|error)\b/i.test(content.slice(0, 300));
  }
  return false;
}

export function pairToolEvents(events) {
  const statuses = new Array((events || []).length).fill(null);
  const pendingCallsByName = {};

  (events || []).forEach((event, index) => {
    if (event?.type === "tool_call") {
      statuses[index] = { kind: "call", state: "running" };
      const name = event.name || "tool";
      (pendingCallsByName[name] ||= []).push(index);
      return;
    }
    if (event?.type === "tool_result") {
      const name = event.name || "tool";
      const queue = pendingCallsByName[name] || [];
      const callIndex = queue.length ? queue.shift() : null;
      const failed = toolResultIsError(event?.content);
      if (callIndex != null) {
        statuses[callIndex].state = failed ? "failed" : "done";
      }
      statuses[index] = { kind: "result", state: failed ? "failed" : "done", callIndex };
      return;
    }
    statuses[index] = { kind: "progress", state: "progress" };
  });

  return statuses;
}

export function extractEditableText(content) {
  const text = String(content || "");
  const markerIndex = text.indexOf(ATTACHMENT_MESSAGE_MARKER);
  const body = markerIndex >= 0 ? text.slice(0, markerIndex) : text;
  return body.trim();
}

export function isErrorMessage(message) {
  if (!message || message.role !== "assistant") return false;
  if (message.error === true) return true;
  const content = String(message.content || "");
  return content.startsWith(ERROR_MESSAGE_PREFIX_EN) || content.startsWith(ERROR_MESSAGE_PREFIX_ZH);
}
