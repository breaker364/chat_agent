// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App, { ChatMessageContent, iconForTool } from "./App";

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

function standardHandler(overrides = {}) {
  return async (url, options = {}) => {
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
  };
}

describe("App visual system", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.localStorage.clear();
    delete document.documentElement.dataset.theme;
  });

  it("does not render an entity-specific badge in the header", async () => {
    renderWithFetch(standardHandler());
    await waitFor(() => expect(screen.getByRole("radiogroup", { name: "主题" })).toBeTruthy());
    expect(screen.queryByText(/12306/)).toBeNull();
  });

  it("shows a skill badge derived from the installed skill list", async () => {
    renderWithFetch(standardHandler({
      "GET /skills": () => jsonResponse({ skills: [
        { name: "alpha", description: "demo alpha" },
        { name: "beta", description: "demo beta" },
      ] }),
    }));
    await waitFor(() => expect(screen.getByText("技能 2")).toBeTruthy());
  });

  it("hides the skill badge when no skills are installed", async () => {
    renderWithFetch(standardHandler());
    await waitFor(() => expect(screen.getByRole("radiogroup", { name: "主题" })).toBeTruthy());
    await waitFor(() => expect(screen.queryByText(/技能/)).toBeNull());
  });

  it("shows a failure toast when knowledge sync fails", async () => {
    renderWithFetch(standardHandler({
      "POST /knowledge/sync": () => jsonResponse({ error: "知识库同步失败" }, { status: 500 }),
    }));
    await waitFor(() => expect(screen.getByRole("button", { name: "同步知识库" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "同步知识库" }));
    await waitFor(() => expect(screen.getByText(/知识库同步失败/)).toBeTruthy());
  });

  it("uses a generic icon mapping that does not depend on entity keywords", () => {
    const byText = (icon) => JSON.stringify(icon?.props ?? {});
    expect(iconForTool("web_search").type).toBe(iconForTool("fetch_page").type);
    expect(iconForTool("read_file").type).toBe(iconForTool("list_dir").type);
    expect(iconForTool("run_shell").type).toBe(iconForTool("bash_command").type);
    const unknown = iconForTool("train_ticket_lookup");
    const fileIcon = iconForTool("read_file");
    expect(unknown.type).not.toBe(fileIcon.type);
    expect(unknown.type.displayName || unknown.type.name || byText(unknown)).toBeTruthy();
  });

  it("renders code blocks with syntax highlighting and a copy button", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const content = "```js\nconst answer = 42;\nconsole.log(answer);\n```";
    render(<ChatMessageContent content={content} />);

    const code = document.querySelector("pre code");
    expect(code).toBeTruthy();
    expect(code.className).toContain("hljs");

    const button = screen.getByRole("button", { name: "复制代码" });
    fireEvent.click(button);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("const answer = 42;\nconsole.log(answer);\n"));
  });
});
