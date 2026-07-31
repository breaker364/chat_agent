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
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function textTransfer(file) {
  return {
    types: ["Files"],
    files: [file],
    items: [
      {
        kind: "file",
        type: file.type,
        getAsFile: () => file,
      },
    ],
  };
}

function renderWithFetch(handler) {
  Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal("fetch", vi.fn(handler));
  render(<App />);
}

describe("App knowledge base controls", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("imports dropped knowledge files and runs manual sync", async () => {
    const calls = [];
    renderWithFetch(async (url, options = {}) => {
      calls.push({ url: String(url), method: options.method || "GET", body: options.body });
      if (url === "/feishu/session") {
        return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
      }
      if (url === "/sessions") return jsonResponse({ sessions: [] });
      if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
      if (url === "/knowledge/import") {
        return jsonResponse({ counts: { imported: 1 }, imported: [{ title: "kb.md" }] });
      }
      if (url === "/knowledge/sync") {
        return jsonResponse({ counts: { indexed: 1, refreshed: 0, unchanged: 0, skipped: 0, failed: 0, deleted: 0 } });
      }
      return jsonResponse({ error: "unexpected request" }, { status: 404 });
    });

    const panel = await screen.findByLabelText("Knowledge base");
    const file = new File(["knowledge body"], "kb.md", { type: "text/markdown", lastModified: 1 });

    fireEvent.drop(panel, { dataTransfer: textTransfer(file) });
    await waitFor(() => expect(calls.some((call) => call.url === "/knowledge/import" && call.method === "POST")).toBe(true));

    fireEvent.click(screen.getByRole("button", { name: /sync knowledge/i }));
    await waitFor(() => expect(calls.some((call) => call.url === "/knowledge/sync" && call.method === "POST")).toBe(true));
    expect(screen.getByText(/indexed 1/i)).toBeTruthy();
  });

  it("sends knowledge_mode when the user enables knowledge-base answering", async () => {
    const calls = [];
    renderWithFetch(async (url, options = {}) => {
      calls.push({ url: String(url), method: options.method || "GET", body: options.body });
      if (url === "/feishu/session") {
        return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
      }
      if (url === "/sessions") return jsonResponse({ sessions: [] });
      if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
      if (url === "/chat/stream") return sseDoneResponse("answered from knowledge");
      return jsonResponse({ error: "unexpected request" }, { status: 404 });
    });

    const textarea = await screen.findByPlaceholderText(/send a message/i);
    fireEvent.click(screen.getByLabelText(/use knowledge base/i));
    fireEvent.change(textarea, { target: { value: "What does my knowledge base say?" } });
    fireEvent.click(screen.getByTitle("Send message"));

    await waitFor(() => expect(calls.some((call) => call.url === "/chat/stream")).toBe(true));
    const chatCall = calls.find((call) => call.url === "/chat/stream");
    expect(JSON.parse(chatCall.body).knowledge_mode).toBe(true);
  });

  it("renders citations returned by knowledge_search tool results", async () => {
    const citationPayload = {
      results: [
        {
          citation_id: "chunk-1",
          chunk_id: "chunk-1",
          collection: "notes",
          source_ref: "Guide.md",
          heading_path: ["Setup"],
          snippet: "Alpha setup requires the documented sync step.",
        },
      ],
    };
    renderWithFetch(async (url) => {
      if (url === "/feishu/session") {
        return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
      }
      if (url === "/sessions") return jsonResponse({ sessions: [{ session_id: "s1", title: "Session" }] });
      if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
      if (url === "/sessions/s1") {
        return jsonResponse({
          session_id: "s1",
          messages: [
            {
              role: "assistant",
              content: "Use the documented sync step.",
              tools: [{ type: "tool_result", name: "knowledge_search", content: JSON.stringify(citationPayload) }],
            },
          ],
          task_progress: {},
          subagent_tasks: [],
          subagent_notifications: [],
          context_stats: {},
        });
      }
      return jsonResponse({ error: "unexpected request" }, { status: 404 });
    });

    expect(await screen.findByText("Knowledge citations")).toBeTruthy();
    expect(screen.getByText(/Guide\.md/)).toBeTruthy();
    expect(screen.getByText(/chunk-1/)).toBeTruthy();
    expect(screen.getByText(/Alpha setup requires/)).toBeTruthy();
  });

  it("opens a knowledge center to browse sources and inspect document chunks", async () => {
    const calls = [];
    renderWithFetch(async (url) => {
      calls.push(String(url));
      if (url === "/feishu/session") {
        return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
      }
      if (url === "/sessions") return jsonResponse({ sessions: [] });
      if (url === "/knowledge/documents") {
        return jsonResponse({
          documents: [
            {
              doc_id: "doc-1",
              collection: "notes",
              title: "policy.md",
              source_uri: "knowledge_base/documents/notes/policy.md",
              source_type: "md",
              status: "indexed",
              chunk_count: 2,
            },
          ],
        });
      }
      if (url === "/knowledge/sources") {
        return jsonResponse({
          sources: [
            {
              doc_id: "doc-1",
              collection: "notes",
              title: "policy.md",
              source_uri: "knowledge_base/documents/notes/policy.md",
              source_type: "md",
              status: "indexed",
              chunk_count: 2,
            },
            {
              doc_id: "",
              collection: "notes",
              title: "draft.md",
              source_uri: "knowledge_base/documents/notes/draft.md",
              source_type: "md",
              status: "pending_sync",
              chunk_count: 0,
            },
          ],
        });
      }
      if (url === "/knowledge/documents/doc-1") {
        return jsonResponse({
          document: {
            doc_id: "doc-1",
            collection: "notes",
            title: "policy.md",
            source_uri: "knowledge_base/documents/notes/policy.md",
            source_type: "md",
            status: "indexed",
            chunk_count: 2,
          },
          chunks: [
            {
              chunk_id: "chunk-1",
              citation_id: "chunk-1",
              text: "Policy chunk text with operational guidance.",
              heading_path: ["Policy"],
              ordinal: 0,
              token_count: 8,
            },
          ],
        });
      }
      return jsonResponse({ error: "unexpected request" }, { status: 404 });
    });

    fireEvent.click(await screen.findByRole("button", { name: /open knowledge center/i }));

    expect(await screen.findByText("Knowledge Center")).toBeTruthy();
    expect(screen.getByText("notes")).toBeTruthy();
    expect(screen.getByText("policy.md")).toBeTruthy();
    expect(screen.getByText("draft.md")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /inspect policy\.md/i }));

    expect(await screen.findByText(/Policy chunk text/)).toBeTruthy();
    expect(screen.getByText(/chunk-1/)).toBeTruthy();
    expect(calls).toContain("/knowledge/documents/doc-1");
  });

  it("deletes pending knowledge source files from the knowledge center", async () => {
    const calls = [];
    renderWithFetch(async (url, options = {}) => {
      calls.push({ url: String(url), method: options.method || "GET" });
      if (url === "/feishu/session") {
        return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
      }
      if (url === "/sessions") return jsonResponse({ sessions: [] });
      if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
      if (url === "/knowledge/sources") {
        return jsonResponse({
          sources: [
            {
              doc_id: "",
              collection: "notes",
              title: "draft.md",
              source_uri: "knowledge_base/documents/notes/draft.md",
              source_type: "md",
              status: "pending_sync",
              chunk_count: 0,
            },
          ],
        });
      }
      if (url === "/knowledge/sources?source_uri=knowledge_base%2Fdocuments%2Fnotes%2Fdraft.md" && options.method === "DELETE") {
        return jsonResponse({ deleted: [], removed_sources: ["knowledge_base/documents/notes/draft.md"] });
      }
      return jsonResponse({ error: "unexpected request" }, { status: 404 });
    });

    fireEvent.click(await screen.findByRole("button", { name: /open knowledge center/i }));
    fireEvent.click(await screen.findByRole("button", { name: /delete draft\.md/i }));

    await waitFor(() => {
      expect(calls).toContainEqual({
        url: "/knowledge/sources?source_uri=knowledge_base%2Fdocuments%2Fnotes%2Fdraft.md",
        method: "DELETE",
      });
    });
  });

  it("shows per-file sync failure details in the knowledge panel", async () => {
    renderWithFetch(async (url, options = {}) => {
      if (url === "/feishu/session") {
        return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
      }
      if (url === "/sessions") return jsonResponse({ sessions: [] });
      if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
      if (url === "/knowledge/sources") return jsonResponse({ sources: [] });
      if (url === "/knowledge/sync" && options.method === "POST") {
        return jsonResponse({
          counts: { indexed: 0, refreshed: 0, unchanged: 0, skipped: 0, failed: 1, deleted: 0 },
          files: [
            {
              path: "knowledge_base/documents/default/bad.pdf",
              collection: "default",
              status: "failed",
              reason: "PDF text extraction produced no text",
            },
          ],
        });
      }
      return jsonResponse({ error: "unexpected request" }, { status: 404 });
    });

    fireEvent.click(await screen.findByRole("button", { name: /sync knowledge/i }));

    expect(await screen.findByText(/bad\.pdf/)).toBeTruthy();
    expect(screen.getByText(/PDF text extraction produced no text/)).toBeTruthy();
  });

  it("collapses knowledge and web login panels to compact headers", async () => {
    renderWithFetch(async (url) => {
      if (url === "/feishu/session") {
        return jsonResponse({ logged_in: true, has_session: true, issued_at: 1785466251, metadata: {} });
      }
      if (url === "/sessions") return jsonResponse({ sessions: [] });
      if (url === "/knowledge/documents") {
        return jsonResponse({
          documents: [
            { doc_id: "doc-1", collection: "default", title: "brief.md", source_uri: "brief.md", chunk_count: 1 },
          ],
        });
      }
      return jsonResponse({ error: "unexpected request" }, { status: 404 });
    });

    fireEvent.click(await screen.findByRole("button", { name: /hide knowledge base panel/i }));
    expect(screen.queryByRole("button", { name: /sync knowledge/i })).toBeNull();
    expect(screen.getByRole("button", { name: /show knowledge base panel/i })).toBeTruthy();
    expect(screen.getByText(/1 documents \/ ready/i)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /hide web login panel/i }));
    expect(screen.queryByRole("button", { name: /init qr/i })).toBeNull();
    expect(screen.getByRole("button", { name: /show web login panel/i })).toBeTruthy();
    expect(screen.getByText(/connected/i)).toBeTruthy();
  });
});
