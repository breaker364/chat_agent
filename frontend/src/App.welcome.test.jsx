// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

function jsonResponse(body, init = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status || 200,
    headers: { "Content-Type": "application/json" },
  });
}

function renderApp(overrides = {}) {
  Element.prototype.scrollIntoView = vi.fn();
  const fetchMock = vi.fn(async (url, options = {}) => {
    const route = `${options.method || "GET"} ${url}`;
    if (overrides[route]) return overrides[route]();
    if (url === "/feishu/session") {
      return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
    }
    if (url === "/sessions") return jsonResponse({ sessions: [] });
    if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
    if (url === "/knowledge/sources") return jsonResponse({ sources: [] });
    if (url === "/skills") return jsonResponse({ skills: overrides.skills || [] });
    if (url === "/skills/available") return jsonResponse({ skills: [] });
    return jsonResponse({ error: "unexpected request" }, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<App />);
  return fetchMock;
}

describe("App welcome empty state", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("shows the welcome guide instead of a default assistant reply on a fresh session", async () => {
    renderApp();

    expect(await screen.findByText("今天要做点什么?")).toBeTruthy();
    expect(screen.getByRole("region", { name: "欢迎引导" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /联网检索/ })).toBeTruthy();
    expect(screen.queryByText(/已就绪/)).toBeNull();
  });

  it("fills the composer with a suggestion instead of sending it", async () => {
    renderApp();
    await screen.findByText("今天要做点什么?");

    const chip = screen.getAllByRole("button", { name: /联网检索/ })[0];
    fireEvent.click(chip);

    const textarea = screen.getByPlaceholderText(/发送消息/);
    expect(textarea.value).toBe(chip.textContent);
    await waitFor(() => expect(document.activeElement).toBe(textarea));
  });

  it("prefers configured suggestions from ui-config", async () => {
    renderApp({
      "GET /ui-config": () => jsonResponse({ welcome_suggestions: ["配置驱动的建议词"] }),
    });

    await screen.findByText("配置驱动的建议词");
    expect(screen.queryByText(/联网检索一个主题/)).toBeNull();
  });

  it("hides the welcome guide once the session has messages", async () => {
    renderApp({
      "GET /sessions": () => jsonResponse({
        sessions: [{ session_id: "s1", title: "有内容会话" }],
      }),
      "GET /sessions/s1": () => jsonResponse({
        session_id: "s1",
        messages: [
          { role: "user", content: "你好", created_at: new Date().toISOString() },
          { role: "assistant", content: "你好,有什么可以帮你?", created_at: new Date().toISOString() },
        ],
        task_progress: {},
        subagent_tasks: [],
        subagent_notifications: [],
        context_stats: {},
      }),
    });

    await screen.findByText("你好,有什么可以帮你?");
    await waitFor(() => expect(screen.queryByText("今天要做点什么?")).toBeNull());
  });

  it("appends skill-based suggestions from the installed skill list", async () => {
    renderApp({
      skills: [{ name: "format-doc", description: "整理文档结构" }],
    });

    await screen.findByText("今天要做点什么?");
    expect(screen.getByRole("button", { name: /format-doc/ })).toBeTruthy();
  });
});
