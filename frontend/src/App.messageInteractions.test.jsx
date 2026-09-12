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

function sseDoneResponse(text = "ok") {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(`event: done\ndata: ${JSON.stringify(text)}\n\n`));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const RECENT_ISO = new Date(Date.now() - 2 * 60 * 1000).toISOString();

function sessionPayload(messages) {
  return {
    session_id: "s1",
    title: "会话",
    created_at: RECENT_ISO,
    updated_at: RECENT_ISO,
    messages,
    task_progress: {},
    subagent_tasks: [],
    subagent_notifications: [],
    context_stats: {},
  };
}

function historyMessages() {
  return [
    { role: "user", content: "帮我查一下天气", created_at: RECENT_ISO },
    { role: "assistant", content: "今天天气晴朗。", created_at: RECENT_ISO },
    {
      role: "user",
      content: "总结这个文件\n\nAttached files available in the workspace:\n- a.md (1.2 KB): `/w/a.md`",
      created_at: RECENT_ISO,
    },
    { role: "assistant", content: "文件的核心结论是成本下降。", created_at: RECENT_ISO },
  ];
}

function renderSession(messages, extraRoutes = {}) {
  Element.prototype.scrollIntoView = vi.fn();
  const fetchMock = vi.fn(async (url, options = {}) => {
    const route = `${options.method || "GET"} ${url}`;
    if (extraRoutes[route]) return extraRoutes[route]();
    if (url === "/feishu/session") {
      return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
    }
    if (url === "/sessions") return jsonResponse({ sessions: [{ session_id: "s1", title: "会话" }] });
    if (url === "/sessions/s1") return jsonResponse(sessionPayload(messages));
    if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
    if (url === "/knowledge/sources") return jsonResponse({ sources: [] });
    if (url === "/skills") return jsonResponse({ skills: [] });
    if (url === "/skills/available") return jsonResponse({ skills: [] });
    if (url === "/chat/stream") return sseDoneResponse("重新生成的回答");
    return jsonResponse({ error: "unexpected request" }, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<App />);
  return fetchMock;
}

describe("App message interactions", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("shows copy and feedback actions on assistant messages", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    renderSession(historyMessages());

    const toolbars = await screen.findAllByRole("toolbar", { name: "消息操作" });
    expect(toolbars.length).toBeGreaterThan(0);
    expect(toolbar).toBeTruthy();

    const copyButtons = screen.getAllByRole("button", { name: "复制回复" });
    fireEvent.click(copyButtons[copyButtons.length - 1]);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("文件的核心结论是成本下降。"));

    fireEvent.click(screen.getAllByRole("button", { name: "有帮助" })[0]);
    await waitFor(() => {
      const store = JSON.parse(window.localStorage.getItem("chat-agent:message-feedback") || "{}");
      expect(JSON.stringify(store)).toContain("up");
    });
  });

  it("offers regenerate only on the last assistant message and resends the last user message", async () => {
    const fetchMock = renderSession(historyMessages());

    await waitFor(() => expect(screen.getAllByRole("button", { name: "重新生成" })).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "重新生成" }));

    await waitFor(() => {
      const chatCall = fetchMock.mock.calls.find(([url]) => url === "/chat/stream");
      expect(chatCall).toBeTruthy();
    });
    const chatCall = fetchMock.mock.calls.find(([url]) => url === "/chat/stream");
    const body = JSON.parse(chatCall[1].body);
    expect(body.message).toContain("总结这个文件");
  });

  it("lets users edit a message back into the composer without changing history", async () => {
    renderSession(historyMessages());
    await screen.findByText("文件的核心结论是成本下降。");

    const editButtons = screen.getAllByRole("button", { name: "编辑消息" });
    fireEvent.click(editButtons[0]);

    const textarea = screen.getByPlaceholderText(/发送消息/);
    expect(textarea.value).toBe("帮我查一下天气");
    expect(screen.getByText("文件的核心结论是成本下降。")).toBeTruthy();
  });

  it("renders relative timestamps on messages", async () => {
    renderSession(historyMessages());
    await screen.findByText("文件的核心结论是成本下降。");
    expect(screen.getAllByText("2 分钟前").length).toBeGreaterThan(0);
  });

  it("reflects real tool call status from paired results", async () => {
    const messages = [
      {
        role: "assistant",
        content: "检索完成。",
        created_at: RECENT_ISO,
        tools: [
          { type: "tool_call", name: "web_search", arguments: {} },
          { type: "tool_result", name: "web_search", content: "sunny" },
          { type: "tool_call", name: "read_file", arguments: {} },
          { type: "tool_result", name: "read_file", content: JSON.stringify({ error: "missing" }) },
        ],
      },
    ];
    renderSession(messages);
    await screen.findByText("检索完成。");

    fireEvent.click(screen.getByText("工具调用").closest("button"));
    await waitFor(() => expect(screen.getAllByText("完成").length).toBeGreaterThanOrEqual(2));
    expect(screen.getAllByText("失败").length).toBeGreaterThanOrEqual(2);
  });

  it("offers a retry action on failed assistant replies", async () => {
    const fetchMock = renderSession(
      [
        { role: "user", content: "触发失败", created_at: RECENT_ISO },
        { role: "assistant", content: "Request failed: HTTP 500", created_at: RECENT_ISO },
      ],
      { "POST /chat/stream": () => sseDoneResponse("重试成功") }
    );

    fireEvent.click(await screen.findByRole("button", { name: "重试" }));
    await waitFor(() => {
      const chatCall = fetchMock.mock.calls.find(([url]) => url === "/chat/stream");
      expect(chatCall).toBeTruthy();
    });
    const chatCall = fetchMock.mock.calls.find(([url]) => url === "/chat/stream");
    expect(JSON.parse(chatCall[1].body).message).toBe("触发失败");
  });

  it("shows a jump-to-bottom control when scrolled away from the latest message", async () => {
    renderSession(historyMessages());
    await screen.findByText("文件的核心结论是成本下降。");

    const scroller = document.querySelector(".app-container");
    expect(screen.queryByRole("button", { name: "回到底部" })).toBeNull();

    Object.defineProperty(scroller, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(scroller, "clientHeight", { value: 200, configurable: true });
    Object.defineProperty(scroller, "scrollTop", { value: 0, configurable: true });
    fireEvent.scroll(scroller);

    await waitFor(() => expect(screen.getByRole("button", { name: "回到底部" })).toBeTruthy());
  });
});
