import { describe, expect, it } from "vitest";

import {
  extractEditableText,
  isErrorMessage,
  pairToolEvents,
  toolResultIsError,
} from "./messageMeta";

describe("toolResultIsError", () => {
  it("detects structured error payloads", () => {
    expect(toolResultIsError(JSON.stringify({ error: "boom" }))).toBe(true);
    expect(toolResultIsError(JSON.stringify({ ok: false }))).toBe(true);
    expect(toolResultIsError({ error: "boom" })).toBe(true);
  });

  it("detects plain-text failures", () => {
    expect(toolResultIsError("Request failed: HTTP 500")).toBe(true);
    expect(toolResultIsError("command failed with exit code 1")).toBe(true);
  });

  it("treats normal results as success", () => {
    expect(toolResultIsError(JSON.stringify({ results: [] }))).toBe(false);
    expect(toolResultIsError("all good")).toBe(false);
  });
});

describe("pairToolEvents", () => {
  it("marks a call without a result as running", () => {
    const statuses = pairToolEvents([{ type: "tool_call", name: "web_search" }]);
    expect(statuses[0].state).toBe("running");
  });

  it("pairs a result with the latest matching call and marks it done", () => {
    const statuses = pairToolEvents([
      { type: "tool_call", name: "web_search" },
      { type: "tool_result", name: "web_search", content: "ok" },
    ]);
    expect(statuses[0].state).toBe("done");
    expect(statuses[1].state).toBe("done");
  });

  it("marks a call as failed when the paired result is an error", () => {
    const statuses = pairToolEvents([
      { type: "tool_call", name: "web_search" },
      { type: "tool_result", name: "web_search", content: JSON.stringify({ error: "boom" }) },
    ]);
    expect(statuses[0].state).toBe("failed");
  });

  it("pairs same-name calls in order", () => {
    const statuses = pairToolEvents([
      { type: "tool_call", name: "read_file" },
      { type: "tool_call", name: "read_file" },
      { type: "tool_result", name: "read_file", content: "first" },
      { type: "tool_result", name: "read_file", content: "second" },
    ]);
    expect(statuses[0].state).toBe("done");
    expect(statuses[1].state).toBe("done");
  });

  it("keeps progress events out of the call pairing", () => {
    const statuses = pairToolEvents([
      { type: "tool_call", name: "web_search" },
      { type: "progress", message: "waiting" },
      { type: "tool_result", name: "web_search", content: "ok" },
    ]);
    expect(statuses[1].kind).toBe("progress");
    expect(statuses[2].callIndex).toBe(0);
  });
});

describe("extractEditableText", () => {
  it("strips the generated attachment trailer", () => {
    const content = "分析这个文件\n\nAttached files available in the workspace:\n- a.md (1.2 KB): `/w/a.md`";
    expect(extractEditableText(content)).toBe("分析这个文件");
  });

  it("keeps plain content untouched", () => {
    expect(extractEditableText("普通问题")).toBe("普通问题");
  });
});

describe("isErrorMessage", () => {
  it("flags assistant error replies", () => {
    expect(isErrorMessage({ role: "assistant", content: "Request failed: HTTP 500" })).toBe(true);
    expect(isErrorMessage({ role: "assistant", content: "请求失败: HTTP 500" })).toBe(true);
    expect(isErrorMessage({ role: "assistant", content: "ok", error: true })).toBe(true);
  });

  it("ignores normal messages", () => {
    expect(isErrorMessage({ role: "assistant", content: "正常回答" })).toBe(false);
    expect(isErrorMessage({ role: "user", content: "Request failed: HTTP 500" })).toBe(false);
  });
});
