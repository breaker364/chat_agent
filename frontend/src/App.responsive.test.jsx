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

function setViewportWidth(width) {
  Object.defineProperty(window, "innerWidth", { value: width, configurable: true, writable: true });
}

function renderWithFetch(handler) {
  Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal("fetch", vi.fn(handler));
  render(<App />);
}

function standardHandler() {
  return async (url) => {
    if (url === "/feishu/session") {
      return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
    }
    if (url === "/sessions") return jsonResponse({ sessions: [] });
    if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
    if (url === "/knowledge/sources") return jsonResponse({ sources: [] });
    if (url === "/skills") return jsonResponse({ skills: [] });
    if (url === "/skills/available") return jsonResponse({ skills: [] });
    return jsonResponse({ error: "unexpected request" }, { status: 404 });
  };
}

describe("App responsive columns", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.localStorage.clear();
    setViewportWidth(1024);
  });

  it("starts with the debug sidebar closed on narrow viewports", async () => {
    setViewportWidth(1100);
    renderWithFetch(standardHandler());

    await waitFor(() => expect(screen.getByRole("heading", { name: "Chat Agent" })).toBeTruthy());
    expect(screen.queryByRole("separator", { name: /resize debug column/i })).toBeNull();
    expect(screen.queryByRole("separator", { name: /resize subagents column/i })).toBeNull();
    expect(screen.getByRole("separator", { name: /resize sessions column/i })).toBeTruthy();
  });

  it("starts with the debug sidebar closed at common laptop widths when stored open", async () => {
    setViewportWidth(1280);
    window.localStorage.setItem("chat-agent:debug-sidebar-collapsed", "0");
    renderWithFetch(standardHandler());
    await waitFor(() => expect(screen.getByRole("heading", { name: "Chat Agent" })).toBeTruthy());
    expect(screen.queryByRole("separator", { name: /resize debug column/i })).toBeNull();
  });

  it("keeps the debug sidebar open by default on wide viewports", async () => {
    setViewportWidth(1500);
    renderWithFetch(standardHandler());

    await waitFor(() => expect(screen.getByRole("heading", { name: "Chat Agent" })).toBeTruthy());
    expect(screen.getByRole("separator", { name: /resize debug column/i })).toBeTruthy();
  });

  it("lets users reopen the debug sidebar on narrow viewports", async () => {
    setViewportWidth(1100);
    renderWithFetch(standardHandler());
    await waitFor(() => expect(screen.getByRole("heading", { name: "Chat Agent" })).toBeTruthy());

    const expandButton = screen.getByTitle("展开调试面板");
    fireEvent.click(expandButton);

    await waitFor(() => expect(screen.getByRole("separator", { name: /resize debug column/i })).toBeTruthy());
  });
});
