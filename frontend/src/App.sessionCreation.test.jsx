// @vitest-environment jsdom

import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

function jsonResponse(body, init = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status || 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("App session creation", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("does not persist a blank New Session when opening with no existing sessions", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    const calls = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url, options = {}) => {
        calls.push({ url: String(url), method: options.method || "GET" });
        if (url === "/feishu/session") {
          return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
        }
        if (url === "/sessions") {
          return jsonResponse({ sessions: [] });
        }
        return jsonResponse({ error: "unexpected request" }, { status: 404 });
      })
    );

    render(<App />);

    await waitFor(() => expect(calls.some((call) => call.url === "/sessions")).toBe(true));
    await new Promise((resolve) => window.setTimeout(resolve, 0));

    expect(calls.filter((call) => call.url === "/sessions" && call.method === "POST")).toEqual([]);
  });
});
