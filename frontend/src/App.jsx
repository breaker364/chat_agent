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
  RefreshCw,
  Zap,
  Code,
  PlusCircle,
  Download,
  Play,
  X,
  Sparkles,
  Expand,
  Minimize,
  LogIn,
  LogOut,
  QrCode,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import "./App.css";

const API_BASE = "";
const LAST_SESSION_STORAGE_KEY = "chat-agent:last-session-id";
const SESSION_SIDEBAR_COLLAPSED_KEY = "chat-agent:session-sidebar-collapsed";
const DEBUG_SIDEBAR_COLLAPSED_KEY = "chat-agent:debug-sidebar-collapsed";
const MAX_HISTORY_ITEMS = 12;
const MAX_HISTORY_ITEM_CHARS = 4000;

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

function compactHistoryContent(text) {
  const value = String(text || "");
  if (value.length <= MAX_HISTORY_ITEM_CHARS) return value;
  const head = value.slice(0, 2000);
  const tail = value.slice(-1200);
  return `${head}\n\n[... omitted ${value.length - head.length - tail.length} chars ...]\n\n${tail}`;
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

function SubagentTaskCard({ task }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="subagent-card">
      <div className="subagent-card-header">
        <div className="subagent-card-title">{task.description || task.agent_id}</div>
        <div className={`subagent-status subagent-status-${task.status || "idle"}`}>{task.status || "idle"}</div>
      </div>
      <div className="subagent-card-meta">{task.subagent_type || "general-purpose"}</div>
      {(task.result || task.error) ? (
        <>
          <button className="debug-toggle" onClick={() => setExpanded((value) => !value)}>
            {expanded ? "hide result" : "show result"}
          </button>
          {expanded ? <pre className="tool-args">{task.result || task.error}</pre> : null}
        </>
      ) : null}
    </div>
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

function FeishuLoginPanel({
  status,
  loginState,
  loading,
  polling,
  onInit,
  onRefresh,
  onLogout,
}) {
  return (
    <section className="feishu-panel">
      <div className="feishu-panel-header">
        <div className="feishu-panel-title">
          <QrCode size={16} />
          <span>Feishu Web Login</span>
        </div>
        <div className={`feishu-status ${status?.logged_in ? "connected" : "disconnected"}`}>
          {status?.logged_in ? "connected" : "not logged in"}
        </div>
      </div>
      <div className="feishu-panel-actions">
        <button className="feishu-btn" onClick={onInit} disabled={loading || polling}>
          <LogIn size={14} />
          <span>{loading ? "loading..." : polling ? "waiting scan..." : "init qr"}</span>
        </button>
        <button className="feishu-btn" onClick={onRefresh}>
          <RefreshCw size={14} />
          <span>status</span>
        </button>
        <button className="feishu-btn danger" onClick={onLogout}>
          <LogOut size={14} />
          <span>logout</span>
        </button>
      </div>
      {loginState?.qr_png_base64 ? (
        <div className="feishu-qr-block">
          <img
            className="feishu-qr-image"
            src={`data:image/png;base64,${loginState.qr_png_base64}`}
            alt="Feishu login QR code"
          />
          <div className="feishu-qr-hint">Scan in Feishu. Backend is polling automatically.</div>
        </div>
      ) : null}
      {loginState?.message ? <div className="feishu-panel-note">{loginState.message}</div> : null}
      {status?.issued_at ? (
        <div className="feishu-panel-note">
          session issued at: {new Date(status.issued_at * 1000).toLocaleString()}
        </div>
      ) : null}
    </section>
  );
}

// ============================================================
// HTML Diagram Renderer (iframe-based)
// ============================================================
function extractHtmlContent(text) {
  if (!text) return null;
  // Try to extract HTML from a markdown code block
  const codeBlockMatch = text.match(/```html\s*\n?([\s\S]*?)```/i);
  if (codeBlockMatch) return codeBlockMatch[1].trim();
  // Check if the text itself starts with HTML
  if (/^\s*<!DOCTYPE html/i.test(text) || /^\s*<html/i.test(text)) return text.trim();
  return null;
}

function hasHtmlContent(text) {
  return extractHtmlContent(text) !== null;
}

function DiagramIframe({ htmlContent, title }) {
  const [expanded, setExpanded] = useState(false);

  if (!htmlContent) return null;

  // Create blob URL for the HTML
  const blob = new Blob([htmlContent], { type: "text/html" });
  const url = URL.createObjectURL(blob);

  return (
    <div className={`diagram-container ${expanded ? "diagram-expanded" : ""}`}>
      <div className="diagram-toolbar">
        <span className="diagram-title">{title || "Architecture Diagram"}</span>
        <div className="diagram-actions">
          <button
            className="diagram-action-btn"
            onClick={() => setExpanded((v) => !v)}
            title={expanded ? "Collapse" : "Expand"}
          >
            {expanded ? <Minimize size={14} /> : <Expand size={14} />}
          </button>
          <a
            className="diagram-action-btn"
            href={url}
            download="architecture-diagram.html"
            title="Download HTML"
          >
            <Download size={14} />
          </a>
        </div>
      </div>
      <div className="diagram-frame-wrapper">
        <iframe
          className="diagram-frame"
          src={url}
          title={title || "Architecture Diagram"}
          sandbox="allow-scripts"
          loading="lazy"
          onLoad={(e) => {
            // Release the blob URL after load to free memory
            // but keep it accessible for the iframe
          }}
        />
      </div>
    </div>
  );
}

// ============================================================
// Skill Popup Component
// ============================================================
function SkillPopup({
  installedSkills,
  availableSkills,
  selectedSkill,
  skillParams,
  skillResult,
  skillLoading,
  skillTab,
  onClose,
  onInstall,
  onUninstall,
  onSelectSkill,
  onParamChange,
  onExecute,
  onTabChange,
}) {
  if (!Array.isArray(installedSkills)) installedSkills = [];
  if (!Array.isArray(availableSkills)) availableSkills = [];

  const htmlContent = skillResult?.type === "success" ? extractHtmlContent(skillResult.content) : null;

  return (
    <div className="skill-overlay" onClick={onClose}>
      <div className="skill-popup" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="skill-popup-header">
          <div className="skill-popup-title">
            <Zap size={18} />
            <span>Skills</span>
          </div>
          <button className="skill-popup-close" onClick={onClose} title="Close">
            <X size={18} />
          </button>
        </div>

        {/* Tabs */}
        <div className="skill-popup-tabs">
          <button
            className={`skill-tab ${skillTab === "installed" ? "active" : ""}`}
            onClick={() => onTabChange("installed")}
          >
            <Download size={14} />
            Installed ({installedSkills.length})
          </button>
          <button
            className={`skill-tab ${skillTab === "available" ? "active" : ""}`}
            onClick={() => onTabChange("available")}
          >
            <PlusCircle size={14} />
            Available ({availableSkills.length})
          </button>
        </div>

        {/* Body */}
        <div className="skill-popup-body">
          {skillTab === "installed" && (
            <div className="skill-list">
              {installedSkills.length === 0 ? (
                <div className="skill-empty">No installed skills. Switch to "Available" to install one.</div>
              ) : (
                installedSkills.map((skill) => (
                  <div
                    key={skill.name}
                    className={`skill-item ${selectedSkill?.name === skill.name ? "selected" : ""}`}
                    onClick={() => onSelectSkill(skill)}
                  >
                    <div className="skill-item-icon">
                      <Code size={16} />
                    </div>
                    <div className="skill-item-content">
                      <div className="skill-item-name">{skill.name}</div>
                      <div className="skill-item-desc">{skill.description}</div>
                    </div>
                    <button
                      className="skill-item-action danger"
                      onClick={(e) => {
                        e.stopPropagation();
                        onUninstall(skill.name);
                      }}
                      title="Uninstall"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))
              )}
            </div>
          )}

          {skillTab === "available" && (
            <div className="skill-list">
              {availableSkills.length === 0 ? (
                <div className="skill-empty">No additional skills available in the registry.</div>
              ) : (
                availableSkills.map((skill) => (
                  <div
                    key={skill.name}
                    className={`skill-item ${selectedSkill?.name === skill.name ? "selected" : ""}`}
                    onClick={() => onSelectSkill(skill)}
                  >
                    <div className="skill-item-icon">
                      <Sparkles size={16} />
                    </div>
                    <div className="skill-item-content">
                      <div className="skill-item-name">{skill.name}</div>
                      <div className="skill-item-desc">{skill.description}</div>
                    </div>
                    <button
                      className="skill-item-action install"
                      onClick={(e) => {
                        e.stopPropagation();
                        onInstall(skill.name);
                      }}
                      title="Install"
                    >
                      <PlusCircle size={14} />
                    </button>
                  </div>
                ))
              )}
            </div>
          )}

          {/* Selected skill param form */}
          {selectedSkill && (
            <div className="skill-params-section">
              <div className="skill-params-header">
                <span>Execute: <strong>{selectedSkill.name}</strong></span>
              </div>
              {selectedSkill.params_schema?.properties &&
                Object.entries(selectedSkill.params_schema.properties).map(([key, prop]) => {
                  const isRequired = (selectedSkill.params_schema.required || []).includes(key);
                  const enumValues = prop.enum;

                  return (
                    <div key={key} className="skill-param-field">
                      <label className="skill-param-label">
                        {key}
                        {isRequired && <span className="skill-required">*</span>}
                        {prop.description && (
                          <span className="skill-param-desc"> — {prop.description}</span>
                        )}
                      </label>
                      {enumValues ? (
                        <div className="skill-param-enum-group">
                          {enumValues.map((val) => (
                            <button
                              key={val}
                              className={`skill-enum-btn ${skillParams[key] === val ? "active" : ""}`}
                              onClick={() => onParamChange(key, val)}
                            >
                              {val}
                            </button>
                          ))}
                        </div>
                      ) : (
                        <textarea
                          className="skill-param-input"
                          value={skillParams[key] || ""}
                          onChange={(e) => onParamChange(key, e.target.value)}
                          placeholder={prop.description || `Enter ${key}...`}
                          rows={key === "system_description" ? 4 : 2}
                        />
                      )}
                    </div>
                  );
                })}

              <button
                className="skill-execute-btn"
                onClick={onExecute}
                disabled={skillLoading}
              >
                {skillLoading ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
                {skillLoading ? "Executing..." : "Execute"}
              </button>

              {/* Result display — show rendered diagram if HTML, otherwise show text */}
              {skillResult && (
                <div className={`skill-result ${skillResult.type}`}>
                  <div className="skill-result-header">
                    {skillResult.type === "success" ? (
                      <><Sparkles size={14} /> Result</>
                    ) : (
                      <><X size={14} /> Error</>
                    )}
                  </div>
                  {htmlContent ? (
                    <DiagramIframe htmlContent={htmlContent} title={selectedSkill?.name} />
                  ) : (
                    <pre className="skill-result-content">{skillResult.content}</pre>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
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

async function readResponsePayload(resp) {
  const rawText = await resp.text();
  if (!rawText) {
    return { ok: false, data: null, text: "" };
  }
  try {
    return { ok: true, data: JSON.parse(rawText), text: rawText };
  } catch {
    return { ok: false, data: null, text: rawText };
  }
}

// ============================================================
// Custom Markdown renderer with diagram support
// ============================================================
function ChatMessageContent({ content }) {
  const htmlContent = useMemo(() => extractHtmlContent(content), [content]);

  // If the content is primarily an HTML diagram, render the diagram and the text
  if (htmlContent) {
    // Get text before and after the HTML block
    const parts = content.split(/```html\s*\n?[\s\S]*?```/i);
    const textBefore = parts[0]?.trim();
    const textAfter = parts[1]?.trim();

    return (
      <div className="message-content">
        {textBefore && <ReactMarkdown remarkPlugins={[remarkGfm]}>{textBefore}</ReactMarkdown>}
        <DiagramIframe htmlContent={htmlContent} title="Architecture Diagram" />
        {textAfter && <ReactMarkdown remarkPlugins={[remarkGfm]}>{textAfter}</ReactMarkdown>}
      </div>
    );
  }

  return (
    <div className="message-content">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  );
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
  const [subagentTasks, setSubagentTasks] = useState([]);
  const [subagentNotifications, setSubagentNotifications] = useState([]);
  const [debugOpen, setDebugOpen] = useState(!readDebugCollapsed());
  const [sessionSidebarCollapsed, setSessionSidebarCollapsed] = useState(readSidebarCollapsed);
  const [shouldAutoScroll, setShouldAutoScroll] = useState(true);
  const chatAreaRef = useRef(null);
  const chatEndRef = useRef(null);
  const abortRef = useRef(null);

  // Skill system state
  const [showSkillPopup, setShowSkillPopup] = useState(false);
  const [installedSkills, setInstalledSkills] = useState([]);
  const [availableSkills, setAvailableSkills] = useState([]);
  const [selectedSkill, setSelectedSkill] = useState(null);
  const [skillParams, setSkillParams] = useState({});
  const [skillResult, setSkillResult] = useState(null);
  const [skillLoading, setSkillLoading] = useState(false);
  const [skillTab, setSkillTab] = useState("installed");
  const [feishuStatus, setFeishuStatus] = useState(null);
  const [feishuLoginState, setFeishuLoginState] = useState(null);
  const [feishuLoading, setFeishuLoading] = useState(false);
  const [feishuPolling, setFeishuPolling] = useState(false);
  const [contextStats, setContextStats] = useState(null);

  const defaultAssistantMessage = useMemo(
    () => ({
      role: "assistant",
      content:
        "Chat Agent is ready. I can search the web, read files, and work with tools — just describe what you need.",
      tools: [],
    }),
    []
  );

  const scrollDown = useCallback((behavior = "auto") => {
    chatEndRef.current?.scrollIntoView({ behavior });
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

  const refreshFeishuStatus = useCallback(async () => {
    try {
      const resp = await fetch(`${API_BASE}/feishu/session`);
      const { ok, data, text } = await readResponsePayload(resp);
      if (!resp.ok) throw new Error((ok && data?.error) || text || `HTTP ${resp.status}`);
      if (!ok || !data) throw new Error("Invalid session status response.");
      setFeishuStatus(data);
      return data;
    } catch {
      const fallback = { logged_in: false, has_session: false, issued_at: null, metadata: {} };
      setFeishuStatus(fallback);
      return fallback;
    }
  }, []);

  const loadSession = useCallback(async (sessionId, options = {}) => {
    const { scrollToBottom = true } = options;
    try {
      const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setActiveSessionId(data.session_id);
      writeLastSessionId(data.session_id);
      setMessages(data.messages?.length ? data.messages : [defaultAssistantMessage]);
      setSubagentTasks(data.subagent_tasks || []);
      setSubagentNotifications(data.subagent_notifications || []);
      setContextStats(data.context_stats || null);
      if (scrollToBottom) {
        setShouldAutoScroll(true);
        requestAnimationFrame(() => scrollDown("auto"));
      }
      return data.session_id;
    } catch {
      setActiveSessionId(sessionId);
      writeLastSessionId(sessionId);
      setContextStats(null);
      return sessionId;
    }
  }, [defaultAssistantMessage, scrollDown]);

  const refreshActiveSession = useCallback(async () => {
    if (!activeSessionId) return null;
    return loadSession(activeSessionId, { scrollToBottom: false });
  }, [activeSessionId, loadSession]);

  useEffect(() => {
    if (!shouldAutoScroll) return;
    scrollDown("auto");
  }, [messages, streamingText, toolEvents, shouldAutoScroll, scrollDown]);

  useEffect(() => {
    writeSidebarCollapsed(sessionSidebarCollapsed);
  }, [sessionSidebarCollapsed]);

  useEffect(() => {
    writeDebugCollapsed(!debugOpen);
  }, [debugOpen]);

  useEffect(() => {
    if (!activeSessionId) return undefined;
    const timer = window.setInterval(async () => {
      try {
        await refreshActiveSession();
      } catch {
        // ignore polling failures
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [activeSessionId, refreshActiveSession]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await refreshFeishuStatus();
        const sessionList = await refreshSessions();
        if (cancelled) return;
        if (sessionList?.length) {
          const rememberedSessionId = readLastSessionId();
          const matchedSession = rememberedSessionId
            ? sessionList.find((session) => session.session_id === rememberedSessionId)
            : null;
          await loadSession((matchedSession || sessionList[0]).session_id, { scrollToBottom: true });
        } else {
          const sessionId = makeSessionId();
          const resp = await fetch(`${API_BASE}/sessions`, {
            method: "POST",
            headers: { "Content-Type": "application/json; charset=utf-8" },
            body: JSON.stringify({ session_id: sessionId }),
          });
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          await refreshSessions();
          await loadSession(sessionId, { scrollToBottom: true });
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
  }, [defaultAssistantMessage, loadSession, refreshFeishuStatus, refreshSessions]);

  const currentHistory = useMemo(
    () =>
      messages
        .filter((m) => m.role === "user" || m.role === "assistant")
        .slice(-MAX_HISTORY_ITEMS)
        .map((m) => ({ role: m.role, content: compactHistoryContent(m.content) })),
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
      await loadSession(sessionId, { scrollToBottom: true });
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
    setSubagentTasks([]);
    setSubagentNotifications([]);
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
        await loadSession(rememberedSessionId, { scrollToBottom: true });
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

  // --- Skill system helpers ---

  const fetchInstalledSkills = useCallback(async () => {
    try {
      const resp = await fetch(`${API_BASE}/skills`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setInstalledSkills(data.skills || []);
    } catch {
      setInstalledSkills([]);
    }
  }, []);

  const fetchAvailableSkills = useCallback(async () => {
    try {
      const resp = await fetch(`${API_BASE}/skills/available`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setAvailableSkills(data.skills || []);
    } catch {
      setAvailableSkills([]);
    }
  }, []);

  const handleInstallSkill = useCallback(async (name) => {
    try {
      const resp = await fetch(`${API_BASE}/skills/install`, {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({ name }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      await fetchInstalledSkills();
      await fetchAvailableSkills();
    } catch (err) {
      console.error("Install skill failed:", err);
    }
  }, [fetchInstalledSkills, fetchAvailableSkills]);

  const handleUninstallSkill = useCallback(async (name) => {
    try {
      const resp = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}`, {
        method: "DELETE",
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      await fetchInstalledSkills();
      await fetchAvailableSkills();
      setSelectedSkill((prev) => (prev?.name === name ? null : prev));
    } catch (err) {
      console.error("Uninstall skill failed:", err);
    }
  }, [fetchInstalledSkills, fetchAvailableSkills]);

  const handleSelectSkill = useCallback((skill) => {
    setSelectedSkill(skill);
    // Initialize params with defaults from schema
    const defaults = {};
    if (skill.params_schema?.properties) {
      for (const [key, prop] of Object.entries(skill.params_schema.properties)) {
        if (prop.default !== undefined) {
          defaults[key] = prop.default;
        } else if (prop.type === "string" && prop.enum?.length) {
          defaults[key] = prop.enum[0];
        } else {
          defaults[key] = "";
        }
      }
    }
    setSkillParams(defaults);
    setSkillResult(null);
  }, []);

  const handleSkillParamChange = useCallback((key, value) => {
    setSkillParams((prev) => ({ ...prev, [key]: value }));
  }, []);

  const handleExecuteSkill = useCallback(async () => {
    if (!selectedSkill) return;
    setSkillLoading(true);
    setSkillResult(null);
    try {
      const resp = await fetch(`${API_BASE}/skills/${encodeURIComponent(selectedSkill.name)}/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({ params: skillParams }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        setSkillResult({ type: "error", content: data.error || `HTTP ${resp.status}` });
      } else {
        setSkillResult({ type: "success", content: data.result });
        // Also add to chat as a skill execution message
        const skillMsg = {
          role: "assistant",
          content: `**Skill: ${selectedSkill.name}**\n\n${data.result}`,
          tools: [
            {
              type: "tool_call",
              name: "skill-execute",
              arguments: { skill: selectedSkill.name, params: skillParams },
            },
            {
              type: "tool_result",
              name: "skill-execute",
              content: data.result,
            },
          ],
        };
        setMessages((prev) => [...prev, skillMsg]);
      }
    } catch (err) {
      setSkillResult({ type: "error", content: `Request failed: ${err.message}` });
    } finally {
      setSkillLoading(false);
    }
  }, [selectedSkill, skillParams]);

  const handleOpenSkillPopup = useCallback(() => {
    setShowSkillPopup(true);
    setSelectedSkill(null);
    setSkillParams({});
    setSkillResult(null);
    fetchInstalledSkills();
    fetchAvailableSkills();
  }, [fetchInstalledSkills, fetchAvailableSkills]);

  const handleCloseSkillPopup = useCallback(() => {
    setShowSkillPopup(false);
    setSelectedSkill(null);
    setSkillParams({});
    setSkillResult(null);
    setSkillLoading(false);
  }, []);

  const handleFeishuInit = useCallback(async () => {
    setFeishuLoading(true);
    try {
      const resp = await fetch(`${API_BASE}/feishu/login/init`, { method: "POST" });
      const { ok, data, text } = await readResponsePayload(resp);
      if (!resp.ok) throw new Error((ok && data?.error) || text || `HTTP ${resp.status}`);
      if (!ok || !data) throw new Error("Invalid login init response.");
      setFeishuLoginState({
        ...data,
        message: "QR created. Waiting for scan confirmation...",
      });
      setFeishuPolling(true);
    } catch (err) {
      setFeishuLoginState({ message: `Init failed: ${err.message}` });
    } finally {
      setFeishuLoading(false);
    }
  }, []);

  const handleFeishuPoll = useCallback(async () => {
    if (!feishuLoginState?.flow_key) return;
    try {
      const resp = await fetch(`${API_BASE}/feishu/login/poll`, {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({ flow_key: feishuLoginState.flow_key }),
      });
      const { ok, data, text } = await readResponsePayload(resp);
      if (!resp.ok) throw new Error((ok && data?.error) || text || `HTTP ${resp.status}`);
      if (!ok || !data) throw new Error("Invalid login poll response.");
      const nextMessage = data.session
        ? "Login completed. Session saved locally."
        : `Polling status: ${data.next_step || data.status || "pending"}`;
      setFeishuLoginState((prev) => ({ ...(prev || {}), ...data, message: nextMessage }));
      await refreshFeishuStatus();
      if (data.session) {
        setFeishuPolling(false);
        return true;
      }
      return false;
    } catch (err) {
      setFeishuLoginState((prev) => ({ ...(prev || {}), message: `Poll failed: ${err.message}` }));
      setFeishuPolling(false);
      return true;
    }
  }, [feishuLoginState, refreshFeishuStatus]);

  const handleFeishuLogout = useCallback(async () => {
    const resp = await fetch(`${API_BASE}/feishu/session`, { method: "DELETE" });
    if (!resp.ok) {
      const { ok, data, text } = await readResponsePayload(resp);
      throw new Error((ok && data?.error) || text || `HTTP ${resp.status}`);
    }
    setFeishuLoginState(null);
    setFeishuPolling(false);
    await refreshFeishuStatus();
  }, [refreshFeishuStatus]);

  useEffect(() => {
    if (!feishuPolling || !feishuLoginState?.flow_key) return undefined;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      if (cancelled) return;
      const shouldStop = await handleFeishuPoll();
      if (shouldStop) {
        window.clearInterval(timer);
      }
    }, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [feishuPolling, feishuLoginState, handleFeishuPoll]);

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
      await loadSession(ensuredSessionId, { scrollToBottom: false });
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
  }, [activeSessionId, currentHistory, input, loading, refreshSessions, loadSession]);

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      // Detect /skill command — open popup instead of sending
      if (input.trim().toLowerCase() === "/skill") {
        e.preventDefault();
        handleOpenSkillPopup();
        return;
      }
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
            <span className="badge"><Zap size={12} /> Skills</span>
            <span className="badge"><TerminalSquare size={12} /> Debug</span>
            {contextStats ? (
              <span className="badge">
                <Code size={12} />
                ctx {contextStats.history_messages} msg / {contextStats.history_token_estimate} tok
              </span>
            ) : null}
          </div>
        </header>

        <div className="top-panels">
          <FeishuLoginPanel
            status={feishuStatus}
            loginState={feishuLoginState}
            loading={feishuLoading}
            polling={feishuPolling}
            onInit={handleFeishuInit}
            onRefresh={refreshFeishuStatus}
            onLogout={handleFeishuLogout}
          />
        </div>

        <main
          className="chat-area"
          ref={chatAreaRef}
          onScroll={() => {
            const node = chatAreaRef.current;
            if (!node) return;
            const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
            setShouldAutoScroll(nearBottom);
          }}
        >
          <div className="messages-container">
            {messages.map((msg, i) => (
              <div key={i} className="message-group">
                <div className={`message ${msg.role === "user" ? "user-message" : "assistant-message"}`}>
                  <div className="message-avatar">
                    {msg.role === "user" ? <User size={16} /> : <Bot size={16} />}
                  </div>
                  <ChatMessageContent content={msg.content} />
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
                <ChatMessageContent content={streamingText} />
                <span className="cursor-blink">|</span>
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
            <button
              className="skill-slash-btn"
              onClick={handleOpenSkillPopup}
              title="Open skills (/skill)"
              disabled={loading}
            >
              <Zap size={16} />
            </button>
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder='Send a message… (type "/skill" for skills)'
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
        events={[
          ...subagentNotifications.map((item) => ({
            message: item.message,
            stage: "subagent_notification",
            elapsed_seconds: null,
            details: item,
          })),
          ...debugEvents,
        ]}
        loading={loading}
        onToggle={() => setDebugOpen((v) => !v)}
        onClear={() => setDebugEvents([])}
      />
      {debugOpen ? (
        <aside className="subagent-sidebar">
          <div className="subagent-sidebar-header">
            <div>
              <div className="debug-sidebar-title">Subagents</div>
              <div className="debug-sidebar-subtitle">{subagentTasks.length} tasks</div>
            </div>
            <button className="session-toolbar-btn" onClick={refreshActiveSession} title="Refresh subagents">
              <RefreshCw size={14} />
            </button>
          </div>
          <div className="subagent-sidebar-body">
            {subagentTasks.length
              ? subagentTasks.map((task) => (
                  <SubagentTaskCard
                    key={task.agent_id}
                    task={task}
                  />
                ))
              : <div className="debug-empty">No subagent tasks yet.</div>}
          </div>
        </aside>
      ) : null}

      {/* Skill Popup */}
      {showSkillPopup && (
        <SkillPopup
          installedSkills={installedSkills}
          availableSkills={availableSkills}
          selectedSkill={selectedSkill}
          skillParams={skillParams}
          skillResult={skillResult}
          skillLoading={skillLoading}
          skillTab={skillTab}
          onClose={handleCloseSkillPopup}
          onInstall={handleInstallSkill}
          onUninstall={handleUninstallSkill}
          onSelectSkill={handleSelectSkill}
          onParamChange={handleSkillParamChange}
          onExecute={handleExecuteSkill}
          onTabChange={setSkillTab}
        />
      )}
    </div>
  );
}
