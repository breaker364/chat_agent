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

  it("imports a Feishu document by connection and refreshes the knowledge center", async () => {
    const calls = [];
    let documentListCalls = 0;
    let sourceListCalls = 0;
    renderWithFetch(async (url, options = {}) => {
      calls.push({ url: String(url), method: options.method || "GET", body: options.body });
      if (url === "/feishu/session") {
        return jsonResponse({ logged_in: true, has_session: true, issued_at: 1785466251, metadata: {} });
      }
      if (url === "/sessions") return jsonResponse({ sessions: [] });
      if (url === "/knowledge/documents") {
        documentListCalls += 1;
        return jsonResponse({ documents: [] });
      }
      if (url === "/knowledge/sources") {
        sourceListCalls += 1;
        return jsonResponse({ sources: [] });
      }
      if (url === "/knowledge/import/feishu" && options.method === "POST") {
        return jsonResponse({
          status: "indexed",
          collection: "project-notes",
          doc_id: "doc-1",
          source_uri: "feishu://document/remote-token",
          source_url: "https://docs.example.test/docx/remote-token",
          title: "Project notes",
          chunk_count: 3,
        });
      }
      return jsonResponse({ error: "unexpected request" }, { status: 404 });
    });

    fireEvent.click(await screen.findByRole("button", { name: /import feishu document/i }));
    fireEvent.change(screen.getByLabelText(/feishu document url or token/i), {
      target: { value: "https://docs.example.test/docx/remote-token" },
    });
    fireEvent.change(screen.getByLabelText(/knowledge collection/i), {
      target: { value: "project-notes" },
    });
    fireEvent.click(screen.getByLabelText(/force refresh/i));
    fireEvent.click(screen.getByRole("button", { name: /add to knowledge base/i }));

    await waitFor(() => {
      expect(calls.some((call) => call.url === "/knowledge/import/feishu" && call.method === "POST")).toBe(true);
    });
    const importCall = calls.find((call) => call.url === "/knowledge/import/feishu");
    expect(JSON.parse(importCall.body)).toEqual({
      reference: "https://docs.example.test/docx/remote-token",
      collection: "project-notes",
      refresh: true,
    });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /import feishu document/i })).toBeNull());
    expect(documentListCalls).toBeGreaterThan(1);
    expect(sourceListCalls).toBeGreaterThan(1);
    expect(screen.getByText(/indexed \/ project notes/i)).toBeTruthy();
  });

  it("blocks Feishu import until the web session is logged in", async () => {
    const calls = [];
    renderWithFetch(async (url, options = {}) => {
      calls.push({ url: String(url), method: options.method || "GET" });
      if (url === "/feishu/session") {
        return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
      }
      if (url === "/sessions") return jsonResponse({ sessions: [] });
      if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
      if (url === "/knowledge/sources") return jsonResponse({ sources: [] });
      return jsonResponse({ error: "unexpected request" }, { status: 404 });
    });

    fireEvent.click(await screen.findByRole("button", { name: /import feishu document/i }));
    expect(screen.getByText(/sign in to Feishu before importing/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /add to knowledge base/i }).disabled).toBe(true);
    expect(calls.some((call) => call.url === "/knowledge/import/feishu")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: /open login panel/i }));
    expect(screen.queryByRole("dialog", { name: /import feishu document/i })).toBeNull();
    expect(screen.getByRole("button", { name: /init qr/i })).toBeTruthy();
  });

  it("sends the default auto knowledge policy on chat requests", async () => {
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
    fireEvent.change(textarea, { target: { value: "What does my knowledge base say?" } });
    fireEvent.click(screen.getByTitle("Send message"));

    await waitFor(() => expect(calls.some((call) => call.url === "/chat/stream")).toBe(true));
    const chatCall = calls.find((call) => call.url === "/chat/stream");
    expect(JSON.parse(chatCall.body).knowledge_policy).toBe("auto");
    expect(JSON.parse(chatCall.body).knowledge_mode).toBeUndefined();
  });

  it("switches among auto, required, and disabled knowledge policies", async () => {
    renderWithFetch(async (url) => {
      if (url === "/feishu/session") return jsonResponse({ logged_in: false, has_session: false, metadata: {} });
      if (url === "/sessions") return jsonResponse({ sessions: [] });
      if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
      return jsonResponse({ error: "unexpected request" }, { status: 404 });
    });

    expect((await screen.findByRole("radio", { name: /auto/i })).checked).toBe(true);
    fireEvent.click(screen.getByRole("radio", { name: /required/i }));
    expect(screen.getByRole("radio", { name: /required/i }).checked).toBe(true);
    fireEvent.click(screen.getByRole("radio", { name: /disabled/i }));
    expect(screen.getByRole("radio", { name: /disabled/i }).checked).toBe(true);
  });

  it("renders bounded research provenance and validation errors from SSE", async () => {
    const calls = [];
    renderWithFetch(async (url, options = {}) => {
      calls.push({ url: String(url), options });
      if (url === "/feishu/session") return jsonResponse({ logged_in: false, has_session: false, metadata: {} });
      if (url === "/sessions") return jsonResponse({ sessions: [] });
      if (url === "/knowledge/documents") return jsonResponse({ documents: [] });
      if (url === "/chat/stream") {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode(
              `event: research\ndata: ${JSON.stringify({
                policy: "auto",
                outcome: "evidence_gap",
                sources_attempted: ["personal_knowledge", "workspace", "web"],
                attempts: { personal_knowledge: 1, workspace: 1, web: 1 },
                budget: { route_transitions_used: 2, route_transitions_limit: 3 },
                citation_counts: { personal_knowledge: 0, workspace: 0, web: 0 },
                planner_reasoning: "must not be rendered",
              })}\n\n`
            ));
            controller.enqueue(encoder.encode(`event: error\ndata: ${JSON.stringify({ code: "invalid_request", message: "Choose a valid policy." })}\n\n`));
            controller.close();
          },
        });
        return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      return jsonResponse({ error: "unexpected request" }, { status: 404 });
    });

    const textarea = await screen.findByPlaceholderText(/send a message/i);
    fireEvent.change(textarea, { target: { value: "Question" } });
    fireEvent.click(screen.getByTitle("Send message"));

    await waitFor(() => expect(screen.getByText(/evidence gap/i)).toBeTruthy());
    expect(screen.getByText(/personal knowledge/i)).toBeTruthy();
    expect(screen.getByText(/workspace/i)).toBeTruthy();
    expect(screen.queryByText(/must not be rendered/i)).toBeNull();
    expect(screen.getAllByText(/choose a valid policy/i).length).toBeGreaterThan(0);
    expect(calls.some((call) => call.url === "/chat/stream")).toBe(true);
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
