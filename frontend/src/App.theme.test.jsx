// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { THEME_STORAGE_KEY } from "./theme";

function jsonResponse(body, init = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status || 200,
    headers: { "Content-Type": "application/json" },
  });
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

describe("App theme controls", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.localStorage.clear();
    delete document.documentElement.dataset.theme;
    document.title = "";
  });

  it("renders a three-option theme toggle and applies dark on selection", async () => {
    renderWithFetch(standardHandler());
    await waitFor(() => expect(screen.getByRole("radiogroup", { name: "主题" })).toBeTruthy());

    const darkOption = screen.getByRole("radio", { name: "暗色" });
    expect(screen.getByRole("radio", { name: "浅色" })).toBeTruthy();
    expect(screen.getByRole("radio", { name: "跟随系统" })).toBeTruthy();

    fireEvent.click(darkOption);

    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });

  it("applies a stored dark preference on mount", async () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");
    renderWithFetch(standardHandler());
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("dark"));
  });

  it("marks the resolved system option as active", async () => {
    window.matchMedia = vi.fn().mockImplementation((query) => ({
      matches: query === "(prefers-color-scheme: dark)",
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    renderWithFetch(standardHandler());
    await waitFor(() => expect(screen.getByRole("radio", { name: "跟随系统" }).checked).toBe(true));
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
