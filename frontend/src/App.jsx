import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Loader2,
  Send,
  Bot,
  User,
  Search,
  FileText,
  Train,
  Globe,
  Clock,
  TerminalSquare,
  PanelRightOpen,
  PanelRightClose,
  Square,
  Plus,
  MessageSquare,
  Pencil,
  Trash2,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import "./App.css";

const API_BASE = "";
const LAST_SESSION_STORAGE_KEY = "chat-agent:last-session-id";
const SESSION_SIDEBAR_COLLAPSED_KEY = "chat-agent:session-sidebar-collapsed";
const DEBUG_SIDEBAR_COLLAPSED_KEY = "chat-agent:debug-sidebar-collapsed";

function makeSessionId() {
  return `session-${Date.now()}`;
}

function readLastSessionId() {
  try {
    return window.localStorage.getItem(LAST_SESSION_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function writeLastSessionId(sessionId) {
  try {
    if (sessionId) {
      window.localStorage.setItem(LAST_SESSION_STORAGE_KEY, sessionId);
    }
  } catch {
    // Ignore localStorage failures.
  }
}

function readSidebarCollapsed() {
  try {
    return window.localStorage.getItem(SESSION_SIDEBAR_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeSidebarCollapsed(collapsed) {
  try {
    window.localStorage.setItem(SESSION_SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    // Ignore localStorage failures.
  }
}

function readDebugCollapsed() {
  try {
    return window.localStorage.getItem(DEBUG_SIDEBAR_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeDebugCollapsed(collapsed) {
  try {
    window.localStorage.setItem(DEBUG_SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    // Ignore localStorage failures.
  }
}

function iconForTool(toolName) {
  const label = (toolName || "").toLowerCase();
  if (label.includes("search")) return <Globe size={14} />;
  if (label.includes("file") || label.includes("list") || label.includes("read")) return <FileText size={14} />;
  if (label.includes("train") || label.includes("ticket") || label.includes("station")) return <Train size={14} />;
  return <Search size={14} />;
}

function formatValue(value) {
  if (value == null || value === "") return "(empty)";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function tryParseJson(value) {
  if (typeof value !== "string") return null;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function ToolCallBubble({ toolName, args }) {
  return (
    <div className="message tool-message">
      <div className="tool-header">
        {iconForTool(toolName)}
        <span className="tool-name">{toolName || "tool"}</span>
        <span className="tool-status">running</span>
      </div>
      <pre className="tool-args">{formatValue(args)}</pre>
    </div>
  );
}

function ToolResultBubble({ toolName, content }) {
  const [expanded, setExpanded] = useState(false);
  const [rawExpanded, setRawExpanded] = useState(false);
  const text = formatValue(content);
  const preview = text.length > 220 ? `${text.slice(0, 220)}...` : text;
  const parsed = tryParseJson(typeof content === "string" ? content : "");
  const isWebFetch = (toolName || "").toLowerCase() === "web_fetch";
  const fetchPreview = typeof parsed?.content_preview === "string" ? parsed.content_preview : "";
  const fetchPreviewShort = fetchPreview.length > 320 ? `${fetchPreview.slice(0, 320)}...` : fetchPreview;

  return (
    <div className="message tool-result-message">
      <button className="tool-result-toggle" onClick={() => setExpanded((v) => !v)}>
        <FileText size={14} />
        <span>{toolName || "tool result"}</span>
        <span className="toggle-arrow">{expanded ? "collapse" : "expand"}</span>
      </button>
      {isWebFetch && parsed ? (
        <div className="tool-result-block">
          <pre className={expanded ? "tool-result-content tool-result-content-fetch" : "tool-result-preview"}>
            {expanded
              ? [
                  parsed.url ? `url: ${parsed.url}` : null,
                  parsed.title ? `title: ${parsed.title}` : null,
                  parsed.code ? `code: ${parsed.code}` : null,
                  parsed.code_text ? `status: ${parsed.code_text}` : null,
                  parsed.prompt ? `prompt: ${parsed.prompt}` : null,
                  parsed.duration_seconds != null ? `duration_seconds: ${parsed.duration_seconds}` : null,
                  "",
                  fetchPreview || text,
                ]
                  .filter(Boolean)
                  .join("\n")
              : fetchPreviewShort || preview}
          </pre>
          {expanded ? (
            <button className="tool-raw-toggle" onClick={() => setRawExpanded((v) => !v)}>
              {rawExpanded ? "hide raw json" : "show raw json"}
            </button>
          ) : null}
          {expanded && rawExpanded ? <pre className="tool-result-content tool-result-raw">{text}</pre> : null}
        </div>
      ) : (
        <pre className={expanded ? "tool-result-content" : "tool-result-preview"}>{expanded ? text : preview}</pre>
      )}
    </div>
  );
}

function ProgressBubble({ message, elapsedSeconds }) {
  return (
    <div className="message progress-message">
      <div className="tool-header">
        <Clock size={14} />
        <span className="tool-name">{message || "Still working..."}</span>
        {elapsedSeconds != null ? <span className="tool-status">{elapsedSeconds}s</span> : null}
      </div>
    </div>
  );
}

function ToolEvents({ events }) {
  if (!events?.length) return null;
  return (
    <div className="tool-events">
      {events.map((evt, i) => {
        if (evt.type === "tool_call") {
          return <ToolCallBubble key={`tc-${i}`} toolName={evt.name} args={evt.arguments} />;
        }
        if (evt.type === "tool_result") {
          return <ToolResultBubble key={`tr-${i}`} toolName={evt.name} content={evt.content} />;
        }
        return <ProgressBubble key={`pg-${i}`} message={evt.message} elapsedSeconds={evt.elapsed_seconds} />;
      })}
    </div>
  );
}

function DebugEventCard({ event }) {
  const [expanded, setExpanded] = useState(false);
  const hasDetails = event.details && Object.keys(event.details).length > 0;

  return (
    <div className="debug-card">
      <div className="debug-card-header">
        <div className="debug-card-title">
          <TerminalSquare size={14} />
          <span>{event.message || "Debug event"}</span>
        </div>
        <div className="debug-card-meta">
          {event.stage ? <span className="debug-stage">{event.stage}</span> : null}
          {event.elapsed_seconds != null ? <span>{event.elapsed_seconds}s</span> : null}
        </div>
      </div>
      {hasDetails ? (
        <>
          <button className="debug-toggle" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "hide details" : "show details"}
          </button>
          {expanded ? <pre className="tool-args">{formatValue(event.details)}</pre> : null}
        </>
      ) : null}
    </div>
  );
}

function DebugSidebar({ open, events, loading, onToggle, onClear }) {
  return (
    <aside className={`debug-sidebar ${open ? "open" : "collapsed"}`}>
      <div className="debug-sidebar-header">
        <button className="sidebar-toggle" onClick={onToggle} title={open ? "Collapse debug panel" : "Expand debug panel"}>
          {open ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}
        </button>
        {open ? (
          <>
            <div>
              <div className="debug-sidebar-title">Debug stream</div>
              <div className="debug-sidebar-subtitle">{loading ? "live events" : "idle"}</div>
            </div>
            <button className="clear-btn" onClick={onClear}>clear</button>
          </>
        ) : null}
      </div>
      {open ? (
        <div className="debug-sidebar-body">
          {events.length ? events.map((event, index) => <DebugEventCard key={`debug-${index}`} event={event} />) : (
            <div className="debug-empty">No debug events yet.</div>
          )}
        </div>
      ) : null}
    </aside>
  );
}

function SessionSidebar({
  sessions,
  activeSessionId,
  collapsed,
  onToggleCollapsed,
  onSelect,
  onCreate,
  onRename,
  onDelete,
}) {
  return (
    <aside className={`session-sidebar ${collapsed ? "collapsed" : ""}`}>
      <div className="session-sidebar-header">
        <div className="session-brand">
          <Bot size={20} />
          {!collapsed ? <span>Chat Agent</span> : null}
        </div>
        <div className="session-sidebar-actions">
          <button className="session-create-btn" onClick={onCreate} title="New session">
            <Plus size={16} />
          </button>
          <button className="session-collapse-btn" onClick={onToggleCollapsed} title={collapsed ? "Expand sessions" : "Collapse sessions"}>
            {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
          </button>
        </div>
      </div>
      {!collapsed ? (
        <div className="session-sidebar-body">
          {sessions.map((session) => (
            <div
              key={session.session_id}
              className={`session-item ${session.session_id === activeSessionId ? "active" : ""}`}
            >
              <button className="session-item-main" onClick={() => onSelect(session.session_id)}>
                <MessageSquare size={16} />
                <div className="session-item-content">
                  <div className="session-item-title">{session.title || session.session_id}</div>
                  <div className="session-item-meta">
                    {session.task_progress?.status === "running" ? "running" : "idle"}
                  </div>
                </div>
              </button>
              <div className="session-item-toolbar">
                <button className="session-toolbar-btn" onClick={(e) => onRename(e, session)} title="Rename session">
                  <Pencil size={14} />
                </button>
                <button className="session-toolbar-btn danger" onClick={(e) => onDelete(e, session)} title="Delete session">
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </aside>
  );
}

function dispatchSseBlock(block, onEvent) {
  const lines = block.split(/\r?\n/);
  let eventType = "";
  const dataLines = [];

  for (const line of lines) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      eventType = line.slice(6).trim();
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  if (eventType) {
    onEvent(eventType, dataLines.join("\n"));
  }
}

function parseSseChunk(buffer, onEvent) {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const parts = normalized.split("\n\n");

  for (let i = 0; i < parts.length - 1; i += 1) {
    if (parts[i].trim()) {
      dispatchSseBlock(parts[i], onEvent);
    }
  }

  return parts[parts.length - 1] || "";
}

export default function App() {
  const [sessions, setSessions] = useState([]);
  const [activeSessionId, setActiveSessionId] = useState("");
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const [toolEvents, setToolEvents] = useState([]);
  const [debugEvents, setDebugEvents] = useState([]);
  const [debugOpen, setDebugOpen] = useState(!readDebugCollapsed());
  const [sessionSidebarCollapsed, setSessionSidebarCollapsed] = useState(readSidebarCollapsed);
  const chatEndRef = useRef(null);
  const abortRef = useRef(null);

  const defaultAssistantMessage = useMemo(
    () => ({
      role: "assistant",
      content: "I am Chat Agent. I can search the web, inspect files, and query 12306 tickets.",
      tools: [],
    }),
    []
  );

  const scrollDown = useCallback(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const resp = await fetch(`${API_BASE}/sessions`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      const nextSessions = data.sessions || [];
      setSessions(nextSessions);
      return nextSessions;
    } catch {
      return null;
    }
  }, []);

  const loadSession = useCallback(async (sessionId) => {
    try {
      const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setActiveSessionId(data.session_id);
      writeLastSessionId(data.session_id);
      setMessages(data.messages?.length ? data.messages : [defaultAssistantMessage]);
      return data.session_id;
    } catch {
      setActiveSessionId(sessionId);
      writeLastSessionId(sessionId);
      return sessionId;
    }
  }, [defaultAssistantMessage]);

  useEffect(() => {
    scrollDown();
  }, [messages, streamingText, toolEvents, scrollDown]);

  useEffect(() => {
    writeSidebarCollapsed(sessionSidebarCollapsed);
  }, [sessionSidebarCollapsed]);

  useEffect(() => {
    writeDebugCollapsed(!debugOpen);
  }, [debugOpen]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const sessionList = await refreshSessions();
        if (cancelled) return;
        if (sessionList?.length) {
          const rememberedSessionId = readLastSessionId();
          const matchedSession = rememberedSessionId
            ? sessionList.find((session) => session.session_id === rememberedSessionId)
            : null;
          await loadSession((matchedSession || sessionList[0]).session_id);
        } else {
          const sessionId = makeSessionId();
          const resp = await fetch(`${API_BASE}/sessions`, {
            method: "POST",
            headers: { "Content-Type": "application/json; charset=utf-8" },
            body: JSON.stringify({ session_id: sessionId }),
          });
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          await refreshSessions();
          await loadSession(sessionId);
        }
      } catch {
        const sessionId = makeSessionId();
        setActiveSessionId(sessionId);
        writeLastSessionId(sessionId);
        setMessages([defaultAssistantMessage]);
        setSessions((prev) => prev);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [defaultAssistantMessage, loadSession, refreshSessions]);

  const currentHistory = useMemo(
    () =>
      messages
        .filter((m) => m.role === "user" || m.role === "assistant")
        .map((m) => ({ role: m.role, content: m.content })),
    [messages]
  );

  const handleCreateSession = useCallback(async () => {
    const sessionId = makeSessionId();
    setActiveSessionId(sessionId);
    writeLastSessionId(sessionId);
    try {
      await fetch(`${API_BASE}/sessions`, {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({ session_id: sessionId }),
      });
      await refreshSessions();
      await loadSession(sessionId);
    } catch {
      setActiveSessionId(sessionId);
      setMessages([defaultAssistantMessage]);
      setSessions((prev) => [
        {
          session_id: sessionId,
          title: "New Session",
          task_progress: { status: "idle" },
        },
        ...prev,
      ]);
    }
    setDebugEvents([]);
    setToolEvents([]);
    setStreamingText("");
  }, [defaultAssistantMessage, loadSession, refreshSessions]);

  const handleRenameSession = useCallback(async (event, session) => {
    event.preventDefault();
    event.stopPropagation();
    const currentTitle = session.title || session.session_id;
    const nextTitle = window.prompt("Rename session", currentTitle);
    if (!nextTitle || nextTitle.trim() === currentTitle) return;
    const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(session.session_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ title: nextTitle.trim() }),
    });
    if (!resp.ok) return;
    await refreshSessions();
  }, [refreshSessions]);

  const handleDeleteSession = useCallback(async (event, session) => {
    event.preventDefault();
    event.stopPropagation();
    const confirmed = window.confirm(`Delete session "${session.title || session.session_id}"?`);
    if (!confirmed) return;
    const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(session.session_id)}`, {
      method: "DELETE",
    });
    if (!resp.ok) return;

    const nextSessions = sessions.filter((item) => item.session_id !== session.session_id);
    setSessions(nextSessions);
    if (session.session_id === activeSessionId) {
      const rememberedSessionId = nextSessions[0]?.session_id || makeSessionId();
      if (nextSessions.length) {
        await loadSession(rememberedSessionId);
      } else {
        setActiveSessionId(rememberedSessionId);
        writeLastSessionId(rememberedSessionId);
        setMessages([defaultAssistantMessage]);
      }
    } else if (readLastSessionId() === session.session_id) {
      if (nextSessions[0]?.session_id) {
        writeLastSessionId(nextSessions[0].session_id);
      }
    }
    await refreshSessions();
  }, [activeSessionId, defaultAssistantMessage, loadSession, refreshSessions, sessions]);

  const handleStop = useCallback(() => {
    if (!abortRef.current) return;
    abortRef.current.abort();
    abortRef.current = null;
    setLoading(false);
    setStreamingText("");
    setToolEvents((prev) => [...prev, { type: "progress", message: "Request stopped by user.", elapsed_seconds: null }]);
    setDebugEvents((prev) => [
      ...prev,
      {
        type: "debug",
        stage: "user_abort",
        message: "Request aborted from UI.",
        elapsed_seconds: null,
        details: {},
      },
    ]);
  }, []);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || loading) return;

    let ensuredSessionId = activeSessionId;
    if (!ensuredSessionId) {
      ensuredSessionId = makeSessionId();
      setActiveSessionId(ensuredSessionId);
      writeLastSessionId(ensuredSessionId);
    }

    const userMsg = { role: "user", content: text, tools: [] };

    setInput("");
    setLoading(true);
    setStreamingText("");
    setToolEvents([]);
    setDebugEvents([]);
    setMessages((prev) => [...prev, userMsg]);

    const controller = new AbortController();
    abortRef.current = controller;

    const runState = { assistantContent: "", tools: [], debug: [] };

    function pushToolEvent(event) {
      runState.tools.push(event);
      setToolEvents([...runState.tools]);
    }

    function pushDebugEvent(event) {
      runState.debug.push(event);
      setDebugEvents([...runState.debug]);
    }

    function dispatchSseEvent(eventType, rawData) {
      if (!eventType || rawData == null || rawData === "") return;
      let parsed;
      try {
        parsed = JSON.parse(rawData);
      } catch {
        parsed = rawData;
      }

      if (eventType === "text") {
        runState.assistantContent += String(parsed);
        setStreamingText(runState.assistantContent);
      } else if (eventType === "progress") {
        pushToolEvent({
          type: "progress",
          message: parsed?.message || "Still working...",
          elapsed_seconds: parsed?.elapsed_seconds,
          active_tool: parsed?.active_tool,
        });
      } else if (eventType === "debug") {
        const { message, elapsed_seconds, stage, ...details } = parsed || {};
        pushDebugEvent({
          type: "debug",
          message: message || "Debug event",
          elapsed_seconds,
          stage,
          details,
        });
      } else if (eventType === "tool_call") {
        pushToolEvent({
          type: "tool_call",
          name: parsed?.name || "tool",
          arguments: parsed?.arguments,
        });
      } else if (eventType === "tool_result") {
        pushToolEvent({
          type: "tool_result",
          name: parsed?.name || "tool",
          content: parsed?.content ?? parsed,
        });
      } else if (eventType === "error") {
        pushDebugEvent({
          type: "debug",
          message: parsed?.message || "Unknown backend error",
          elapsed_seconds: parsed?.elapsed_seconds,
          stage: "backend_error",
          details: parsed || {},
        });
      } else if (eventType === "done") {
        runState.assistantContent = String(parsed || runState.assistantContent);
        setStreamingText(runState.assistantContent);
      }
    }

    try {
      const resp = await fetch(`${API_BASE}/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({
          message: text,
          session_id: ensuredSessionId,
          history: currentHistory,
        }),
        signal: controller.signal,
      });

      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      if (!resp.body) throw new Error("Response body is empty");

      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        buffer = parseSseChunk(buffer, dispatchSseEvent);
      }

      buffer += decoder.decode();
      if (buffer.trim()) {
        parseSseChunk(`${buffer}\n\n`, dispatchSseEvent);
      }

      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: runState.assistantContent || "(no text reply)",
          tools: runState.tools,
        },
      ]);
      await refreshSessions();
    } catch (err) {
      if (err.name === "AbortError") {
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: runState.assistantContent || "(stopped)", tools: runState.tools },
        ]);
      } else {
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: `Request failed: ${err.message}`, tools: runState.tools },
        ]);
        pushDebugEvent({
          type: "debug",
          stage: "frontend_error",
          message: `Frontend request failed: ${err.message}`,
          elapsed_seconds: null,
          details: {},
        });
      }
    } finally {
      setLoading(false);
      setStreamingText("");
      setToolEvents([]);
      abortRef.current = null;
    }
  }, [activeSessionId, currentHistory, input, loading, refreshSessions]);

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="app-shell">
      <SessionSidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        collapsed={sessionSidebarCollapsed}
        onToggleCollapsed={() => setSessionSidebarCollapsed((prev) => !prev)}
        onSelect={loadSession}
        onCreate={handleCreateSession}
        onRename={handleRenameSession}
        onDelete={handleDeleteSession}
      />

      <div className="app-container">
        <header className="app-header">
          <div className="header-left">
            <Bot size={22} />
            <h1>Chat Agent</h1>
          </div>
          <div className="header-badges">
            <span className="badge"><Globe size={12} /> Search</span>
            <span className="badge"><FileText size={12} /> Files</span>
            <span className="badge"><Train size={12} /> 12306</span>
            <span className="badge"><TerminalSquare size={12} /> Debug</span>
          </div>
        </header>

        <main className="chat-area">
          <div className="messages-container">
            {messages.map((msg, i) => (
              <div key={i} className="message-group">
                <div className={`message ${msg.role === "user" ? "user-message" : "assistant-message"}`}>
                  <div className="message-avatar">
                    {msg.role === "user" ? <User size={16} /> : <Bot size={16} />}
                  </div>
                  <div className="message-content">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                  </div>
                </div>
                {msg.role === "assistant" && <ToolEvents events={msg.tools} />}
              </div>
            ))}

            <ToolEvents events={toolEvents} />

            {loading && streamingText && (
              <div className="message assistant-message streaming">
                <div className="message-avatar">
                  <Bot size={16} />
                </div>
                <div className="message-content">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{streamingText}</ReactMarkdown>
                  <span className="cursor-blink">|</span>
                </div>
              </div>
            )}

            {loading && !streamingText && !toolEvents.length && (
              <div className="message assistant-message">
                <div className="message-avatar">
                  <Bot size={16} />
                </div>
                <div className="message-content">
                  <Loader2 className="spin" size={18} />
                </div>
              </div>
            )}

            <div ref={chatEndRef} />
          </div>
        </main>

        <footer className="input-area">
          <div className="input-wrapper">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Type a message and press Enter..."
              rows={1}
              disabled={loading}
            />
            {loading ? (
              <button className="stop-btn" onClick={handleStop} title="Stop current request">
                <Square size={16} />
              </button>
            ) : null}
            <button className="send-btn" onClick={handleSend} disabled={loading || !input.trim()}>
              {loading ? <Loader2 className="spin" size={18} /> : <Send size={18} />}
            </button>
          </div>
        </footer>
      </div>

      <DebugSidebar
        open={debugOpen}
        events={debugEvents}
        loading={loading}
        onToggle={() => setDebugOpen((v) => !v)}
        onClear={() => setDebugEvents([])}
      />
    </div>
  );
}
