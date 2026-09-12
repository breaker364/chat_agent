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

const NOW_ISO = new Date().toISOString();
const DAY_MS = 24 * 60 * 60 * 1000;

function sessionsList() {
  return {
    sessions: [
      { session_id: "s-today", title: "今日调色板", updated_at: NOW_ISO },
      {
        session_id: "s-yesterday",
        title: "昨日部署复盘",
        updated_at: new Date(Date.now() - DAY_MS).toISOString(),
      },
      {
        session_id: "s-early",
        title: "年初规划笔记",
        updated_at: new Date(Date.now() - 40 * DAY_MS).toISOString(),
      },
    ],
  };
}

function renderApp(overrides = {}) {
  Element.prototype.scrollIntoView = vi.fn();
  const fetchMock = vi.fn(async (url, options = {}) => {
    const route = `${options.method || "GET"} ${url}`;
    if (overrides[route]) return overrides[route]();
    if (url === "/feishu/session") {
      return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
    }
    if (url === "/sessions") return jsonResponse(sessionsList());
    if (url === "/sessions/s-today") {
      return jsonResponse({
        session_id: "s-today",
        messages: [{ role: "user", content: "hello", created_at: NOW_ISO }],
        task_progress: {},
        subagent_tasks: [],
        subagent_notifications: [],
        context_stats: {},
      });
    }
    if (url.startsWith("/sessions/s-")) {
      return jsonResponse({
        session_id: url.split("/")[2],
        messages: [],
        task_progress: {},
        subagent_tasks: [],
        subagent_notifications: [],
        context_stats: {},
      });
    }
    if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
    if (url === "/knowledge/sources") return jsonResponse({ sources: [] });
    if (url === "/skills") return jsonResponse({ skills: [] });
    if (url === "/skills/available") return jsonResponse({ skills: [] });
    return jsonResponse({ error: "unexpected request" }, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<App />);
  return fetchMock;
}

describe("App session management", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("filters sessions by search keyword", async () => {
    renderApp();
    await screen.findByText("今日调色板");

    fireEvent.change(screen.getByPlaceholderText("搜索会话"), { target: { value: "复盘" } });
    expect(screen.getByText("昨日部署复盘")).toBeTruthy();
    expect(screen.queryByText("今日调色板")).toBeNull();
    expect(screen.queryByText("年初规划笔记")).toBeNull();
  });

  it("groups sessions by relative date with labels", async () => {
    renderApp();
    await screen.findByText("今日调色板");
    expect(screen.getByText("今天")).toBeTruthy();
    expect(screen.getByText("昨天")).toBeTruthy();
    expect(screen.getByText("更早")).toBeTruthy();
  });

  it("renames a session through an in-app dialog", async () => {
    const fetchMock = renderApp({
      "PATCH /sessions/s-yesterday": () => jsonResponse({ ok: true }),
    });
    await screen.findByText("昨日部署复盘");

    fireEvent.click(screen.getAllByTitle("重命名会话").find((node) => node.closest(".session-item")?.textContent.includes("昨日部署复盘")));

    const dialog = await screen.findByRole("dialog", { name: "重命名会话" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    const input = screen.getByLabelText("会话标题");
    expect(input.value).toBe("昨日部署复盘");
    expect(document.activeElement).toBe(input);

    fireEvent.change(input, { target: { value: "新的标题" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      const patchCall = fetchMock.mock.calls.find(([url, options]) => url === "/sessions/s-yesterday" && options?.method === "PATCH");
      expect(patchCall).toBeTruthy();
      expect(JSON.parse(patchCall[1].body)).toEqual({ title: "新的标题" });
    });
  });

  it("requires explicit confirmation with the session title before deleting", async () => {
    const fetchMock = renderApp({
      "DELETE /sessions/s-yesterday": () => jsonResponse({ ok: true }),
    });
    await screen.findByText("昨日部署复盘");

    const deleteButtons = screen.getAllByTitle("删除会话");
    const target = deleteButtons.find((node) => node.closest(".session-item")?.textContent.includes("昨日部署复盘"));
    fireEvent.click(target);

    const dialog = await screen.findByRole("dialog", { name: "删除会话" });
    expect(screen.getAllByText(/昨日部署复盘/).length).toBeGreaterThanOrEqual(1);

    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(fetchMock.mock.calls.some(([url, options]) => url === "/sessions/s-yesterday" && options?.method === "DELETE")).toBe(false);

    fireEvent.click(target);
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([url, options]) => url === "/sessions/s-yesterday" && options?.method === "DELETE")).toBe(true);
    });
  });

  it("persists a draft session rename by creating it via POST", async () => {
    const fetchMock = renderApp({
      "PATCH /sessions/s-today": () => jsonResponse({ error: "Session not found" }, { status: 404 }),
      "POST /sessions": () => jsonResponse({ ok: true }),
    });
    await screen.findByText("年初规划笔记");

    const renameButtons = screen.getAllByTitle("重命名会话");
    const draftButton = renameButtons.find((node) => !node.closest(".session-item")?.textContent.match(/调色板|复盘|规划/));
    fireEvent.click(draftButton || renameButtons[0]);

    const dialog = await screen.findByRole("dialog", { name: "重命名会话" });
    fireEvent.change(screen.getByLabelText("会话标题"), { target: { value: "草稿新名字" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(([url, options]) => url === "/sessions" && options?.method === "POST");
      expect(postCall).toBeTruthy();
      const body = JSON.parse(postCall[1].body);
      expect(body.title).toBe("草稿新名字");
      expect(body.session_id).toBe("s-today");
    });
  });

  it("closes dialogs with Escape", async () => {
    renderApp();
    await screen.findByText("昨日部署复盘");

    fireEvent.click(screen.getAllByTitle("重命名会话")[0]);
    expect(await screen.findByRole("dialog", { name: "重命名会话" })).toBeTruthy();

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "重命名会话" })).toBeNull());
  });
});
