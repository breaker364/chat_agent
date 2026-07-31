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

function renderWithFetch(handler) {
  Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal("fetch", vi.fn(handler));
  render(<App />);
}

describe("App responsive column layout", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("lets users resize visible desktop columns with drag handles", async () => {
    renderWithFetch(async (url) => {
      if (url === "/feishu/session") {
        return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
      }
      if (url === "/sessions") return jsonResponse({ sessions: [] });
      if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
      return jsonResponse({ error: "unexpected request" }, { status: 404 });
    });

    await waitFor(() => expect(screen.getByRole("heading", { name: "Chat Agent" })).toBeTruthy());

    const shell = document.querySelector(".app-shell");
    const sessionsHandle = screen.getByRole("separator", { name: /resize sessions column/i });
    fireEvent.pointerDown(sessionsHandle, { clientX: 280, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 340, pointerId: 1 });
    fireEvent.pointerUp(window, { clientX: 340, pointerId: 1 });

    expect(shell.style.getPropertyValue("--session-sidebar-width")).toBe("340px");

    const debugHandle = screen.getByRole("separator", { name: /resize debug column/i });
    fireEvent.pointerDown(debugHandle, { clientX: 900, pointerId: 2 });
    fireEvent.pointerMove(window, { clientX: 840, pointerId: 2 });
    fireEvent.pointerUp(window, { clientX: 840, pointerId: 2 });

    expect(shell.style.getPropertyValue("--debug-sidebar-width")).toBe("420px");
  });
});
