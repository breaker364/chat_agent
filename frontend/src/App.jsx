import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Loader2,
  Send,
  Bot,
  User,
  Search,
  FileText,
  Globe,
  Clock,
  TerminalSquare,
  FileImage,
  PanelRightOpen,
  PanelRightClose,
  Square,
  Plus,
  MessageSquare,
  Pencil,
  Trash2,
  ChevronDown,
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
  Paperclip,
  Link2,
  AlertCircle,
  Wrench,
  Check,
  Copy,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import ToastHost from "./ToastHost";
import { showToast } from "./toast";
import { AssistantMessageActions, UserMessageActions } from "./MessageActions";
import { extractEditableText, isErrorMessage, pairToolEvents } from "./messageMeta";
import { formatFullTime, formatRelativeTime } from "./relativeTime";
import { readFeedbackStore, reportFeedback, toggleFeedback } from "./messageFeedback";
import { ConfirmDialog, PromptDialog } from "./Dialog";
import { groupSessionsByDate } from "./sessionGroups";
import { initialDebugSidebarOpen } from "./layoutPrefs";
import Welcome from "./Welcome";
import { buildWelcomeSuggestions } from "./welcomeSuggestions";
import {
  appendRunText,
  applyRunAttribution,
  finishSessionRun,
  getAppendRequestPayload,
  getSubmitEndpoint,
  getSubmitMode,
  getSessionRun,
  isSessionRunning,
  recordAppendCommandEvent,
  replaceRunEvents,
  setRunText,
  shouldClearComposerAfterSubmit,
  startSessionRun,
  updateSessionRun,
} from "./sessionRunState";
import "./App.css";
import { applyTheme, readThemePreference, setThemePreference, subscribeSystemTheme, THEME_OPTIONS, updateDocumentTitle } from "./theme";

const API_BASE = "";
const LAST_SESSION_STORAGE_KEY = "chat-agent:last-session-id";
const SESSION_SIDEBAR_COLLAPSED_KEY = "chat-agent:session-sidebar-collapsed";
const DEBUG_SIDEBAR_COLLAPSED_KEY = "chat-agent:debug-sidebar-collapsed";
const KNOWLEDGE_PANEL_COLLAPSED_KEY = "chat-agent:knowledge-panel-collapsed";
const FEISHU_PANEL_COLLAPSED_KEY = "chat-agent:feishu-panel-collapsed";
const SESSION_SIDEBAR_WIDTH_KEY = "chat-agent:session-sidebar-width";
const DEBUG_SIDEBAR_WIDTH_KEY = "chat-agent:debug-sidebar-width";
const SUBAGENT_SIDEBAR_WIDTH_KEY = "chat-agent:subagent-sidebar-width";
const MAX_HISTORY_ITEMS = 12;
const MAX_HISTORY_ITEM_CHARS = 4000;
const MAX_ATTACHMENTS = 10;
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
const COMPOSER_MAX_HEIGHT = 176;
const SESSION_SIDEBAR_DEFAULT_WIDTH = 280;
const SESSION_SIDEBAR_MIN_WIDTH = 180;
const SESSION_SIDEBAR_MAX_WIDTH = 420;
const DEBUG_SIDEBAR_DEFAULT_WIDTH = 360;
const DEBUG_SIDEBAR_MIN_WIDTH = 260;
const DEBUG_SIDEBAR_MAX_WIDTH = 560;
const SUBAGENT_SIDEBAR_DEFAULT_WIDTH = 300;
const SUBAGENT_SIDEBAR_MIN_WIDTH = 240;
const SUBAGENT_SIDEBAR_MAX_WIDTH = 480;
const IMAGE_MIME_PREFIX = "image/";
const IMAGE_EXTENSION_BY_TYPE = {
  "image/bmp": "bmp",
  "image/gif": "gif",
  "image/heic": "heic",
  "image/heif": "heif",
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/svg+xml": "svg",
  "image/tiff": "tif",
  "image/webp": "webp",
};

function formatFileSize(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function isImageFile(file) {
  return String(file?.type || "").toLowerCase().startsWith(IMAGE_MIME_PREFIX);
}

function imageExtensionFromType(type) {
  const normalizedType = String(type || "").toLowerCase();
  if (IMAGE_EXTENSION_BY_TYPE[normalizedType]) return IMAGE_EXTENSION_BY_TYPE[normalizedType];
  const subtype = normalizedType.startsWith(IMAGE_MIME_PREFIX)
    ? normalizedType.slice(IMAGE_MIME_PREFIX.length).split(/[+;]/)[0]
    : "";
  return subtype || "png";
}

function ensureImageFileName(file) {
  if (!isImageFile(file) || file.name) return file;
  const extension = imageExtensionFromType(file.type);
  return new File([file], `pasted-image-${Date.now()}.${extension}`, {
    type: file.type,
    lastModified: file.lastModified,
  });
}

function filesFromDataTransfer(dataTransfer) {
  return Array.from(dataTransfer?.files || []);
}

function imageFilesFromClipboard(clipboardData) {
  const itemFiles = Array.from(clipboardData?.items || [])
    .filter((item) => item.kind === "file" && String(item.type || "").toLowerCase().startsWith(IMAGE_MIME_PREFIX))
    .map((item) => item.getAsFile())
    .filter(isImageFile);
  const directFiles = filesFromDataTransfer(clipboardData).filter(isImageFile);
  return (itemFiles.length ? itemFiles : directFiles).map(ensureImageFileName);
}

function dataTransferHasImage(dataTransfer) {
  const items = Array.from(dataTransfer?.items || []);
  if (items.some((item) => String(item.type || "").toLowerCase().startsWith(IMAGE_MIME_PREFIX))) return true;
  return filesFromDataTransfer(dataTransfer).some(isImageFile);
}

function buildAttachmentMessage(text, uploadedFiles) {
  const body = text.trim() || "Read the attached files and complete the requested analysis.";
  if (!uploadedFiles.length) return body;
  const lines = uploadedFiles.map(
    (file) => `- ${file.name} (${formatFileSize(file.size)}): \`${file.path}\``
  );
  return `${body}\n\nAttached files available in the workspace:\n${lines.join("\n")}\n\nRead the relevant files directly before answering.`;
}

function makeSessionId() {
  return `session-${Date.now()}`;
}

function makeDraftSession(sessionId) {
  return {
    session_id: sessionId,
    title: "新会话",
    message_count: 0,
    is_draft: true,
    task_progress: { status: "idle" },
  };
}

function replaceDraftSession(sessions, sessionId) {
  const draft = makeDraftSession(sessionId);
  return [draft, ...(sessions || []).filter((session) => !session.is_draft && session.session_id !== sessionId)];
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

function readKnowledgeCollapsed() {
  try {
    return window.localStorage.getItem(KNOWLEDGE_PANEL_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeKnowledgeCollapsed(collapsed) {
  try {
    window.localStorage.setItem(KNOWLEDGE_PANEL_COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    // Ignore localStorage failures.
  }
}

function readFeishuCollapsed() {
  try {
    return window.localStorage.getItem(FEISHU_PANEL_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeFeishuCollapsed(collapsed) {
  try {
    window.localStorage.setItem(FEISHU_PANEL_COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    // Ignore localStorage failures.
  }
}

function clampNumber(value, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return min;
  return Math.min(max, Math.max(min, Math.round(parsed)));
}

function readStoredWidth(key, fallback, min, max) {
  try {
    const stored = window.localStorage.getItem(key);
    return stored == null ? fallback : clampNumber(stored, min, max);
  } catch {
    return fallback;
  }
}

function writeStoredWidth(key, width) {
  try {
    window.localStorage.setItem(key, String(width));
  } catch {
    // Ignore localStorage failures.
  }
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function formatPercent(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "n/a";
  return `${Math.round(value * 1000) / 10}%`;
}

function formatTokenCount(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "0";
  return new Intl.NumberFormat("en-US").format(value);
}

function formatTurn(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "0turn";
  return `${Math.min(1, Math.max(0, value))}turn`;
}

function toolIconCategory(toolName) {
  const label = (toolName || "").toLowerCase();
  if (/(search|web|fetch|browse|http|url|net)/.test(label)) return "network";
  if (/(file|list|read|write|edit|glob|grep|doc|dir)/.test(label)) return "file";
  if (/(shell|bash|terminal|command|exec|skill|python|script)/.test(label)) return "shell";
  return "generic";
}

export function iconForTool(toolName) {
  const category = toolIconCategory(toolName);
  if (category === "network") return <Globe size={14} />;
  if (category === "file") return <FileText size={14} />;
  if (category === "shell") return <TerminalSquare size={14} />;
  return <Wrench size={14} />;
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

function MessageTokenUsage({ role, usage }) {
  if (!usage || typeof usage !== "object") return null;

  if (role === "user") {
    const contentTokens = usage.content_tokens;
    if (typeof contentTokens !== "number" || !Number.isFinite(contentTokens)) return null;
    return (
      <div className="message-token-usage" title="消息内容 token 统计">
        tokens {formatTokenCount(contentTokens)}
      </div>
    );
  }

  const inputTokens = usage.input_tokens || usage.prompt_tokens || 0;
  const outputTokens = usage.output_tokens || usage.completion_tokens || 0;
  const cacheHitTokens = usage.prompt_cache_hit_tokens || 0;
  const cacheMissTokens = usage.prompt_cache_miss_tokens || 0;
  const cacheTotalTokens = usage.prompt_cache_total_tokens || cacheHitTokens + cacheMissTokens;
  const totalTokens = usage.total_tokens || inputTokens + outputTokens;
  const hasAny =
    inputTokens > 0 ||
    outputTokens > 0 ||
    totalTokens > 0 ||
    cacheHitTokens > 0 ||
    cacheMissTokens > 0;
  if (!hasAny) return null;

  return (
    <div
      className="message-token-usage"
      title={`Input ${formatTokenCount(inputTokens)} | Output ${formatTokenCount(outputTokens)} | Cache hit ${formatTokenCount(cacheHitTokens)} / miss ${formatTokenCount(cacheMissTokens)}`}
    >
      in {formatTokenCount(inputTokens)} · out {formatTokenCount(outputTokens)} · cache hit{" "}
      {formatTokenCount(cacheHitTokens)}
      {cacheTotalTokens ? `/${formatTokenCount(cacheTotalTokens)}` : ""} · total {formatTokenCount(totalTokens)}
    </div>
  );
}

const TOOL_STATUS_LABELS = { running: "运行中", done: "完成", failed: "失败" };

function ToolCallBubble({ toolName, args, status = "running" }) {
  const [expanded, setExpanded] = useState(false);
  const text = formatValue(args);
  const preview = text.length > 120 ? `${text.slice(0, 120)}...` : text;

  return (
    <div className="message tool-message">
      <button className="tool-result-toggle" onClick={() => setExpanded((value) => !value)}>
        {iconForTool(toolName)}
        <span className="tool-name">{toolName || "工具"}</span>
        <span className={`tool-status tool-status-${status}`}>
          {status === "running" ? <Loader2 className="spin" size={11} /> : status === "done" ? <Check size={11} /> : <X size={11} />}
          {TOOL_STATUS_LABELS[status] || TOOL_STATUS_LABELS.running}
        </span>
        <span className="toggle-arrow">{expanded ? "收起" : "展开"}</span>
      </button>
      {expanded ? <pre className="tool-args">{text}</pre> : <div className="tool-collapsed-preview">{preview}</div>}
    </div>
  );
}

function ToolResultBubble({ toolName, content, state = "done" }) {
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
        <span>{toolName || "工具结果"}</span>
        <span className={`tool-status tool-status-${state}`}>
          {state === "failed" ? <X size={11} /> : <Check size={11} />}
          {TOOL_STATUS_LABELS[state] || TOOL_STATUS_LABELS.done}
        </span>
        <span className="toggle-arrow">{expanded ? "收起" : "展开"}</span>
      </button>
      {isWebFetch && parsed ? (
        <div className="tool-result-block">
          {expanded ? (
            <pre className="tool-result-content tool-result-content-fetch">
              {[
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
                .join("\n")}
            </pre>
          ) : (
            <div className="tool-collapsed-preview">{fetchPreviewShort || preview}</div>
          )}
          {expanded ? (
            <button className="tool-raw-toggle" onClick={() => setRawExpanded((v) => !v)}>
              {rawExpanded ? "隐藏原始 JSON" : "显示原始 JSON"}
            </button>
          ) : null}
          {expanded && rawExpanded ? <pre className="tool-result-content tool-result-raw">{text}</pre> : null}
        </div>
      ) : (
        expanded ? <pre className="tool-result-content">{text}</pre> : <div className="tool-collapsed-preview">{preview}</div>
      )}
    </div>
  );
}

function ProgressBubble({ message, elapsedSeconds }) {
  return (
    <div className="message progress-message">
      <div className="tool-header">
        <Clock size={14} />
        <span className="tool-name">{message || "仍在工作中..."}</span>
        {elapsedSeconds != null ? <span className="tool-status">{elapsedSeconds}s</span> : null}
      </div>
    </div>
  );
}

export function mergeActivityItems(items, activity) {
  if (!activity?.state) return items;
  const next = [...items];
  const last = next[next.length - 1];
  if (last && last.state === activity.state && ["working", "judging"].includes(activity.state)) {
    next[next.length - 1] = activity;
  } else {
    next.push(activity);
  }
  return next;
}

function AgentActivityTimeline({ items }) {
  if (!items?.length) return null;
  return <section className="agent-activity" aria-label="Agent 工作动态">
    <div className="agent-activity-title">Agent 工作动态</div>
    {items.map((item, index) => <div className={`agent-activity-item ${item.state || "working"}`} key={`${item.state}-${index}`}>
      <div className="agent-activity-summary">{item.summary}</div>
      {item.evidence ? <div>已确认：{item.evidence}</div> : null}
      {item.judgment ? <div>判断：{item.judgment}</div> : null}
      {item.next_step ? <div>下一步：{item.next_step}</div> : null}
      {item.progress ? <div>进度：{item.progress.current}/{item.progress.total}{item.progress.label ? ` · ${item.progress.label}` : ""}</div> : null}
    </div>)}
  </section>;
}

function ToolEvents({ events }) {
  const [expanded, setExpanded] = useState(false);
  if (!events?.length) return null;
  const toolCalls = events.filter((event) => event.type === "tool_call").length;
  const toolResults = events.filter((event) => event.type === "tool_result").length;
  const progressItems = events.length - toolCalls - toolResults;
  const latest = events[events.length - 1];
  const latestLabel = latest?.name || latest?.message || latest?.type || "tool activity";
  const statuses = pairToolEvents(events);

  return (
    <div className="tool-events">
      <div className="message tool-group-message">
        <button className="tool-result-toggle tool-group-toggle" onClick={() => setExpanded((value) => !value)}>
          <TerminalSquare size={14} />
          <span className="tool-name">工具调用</span>
          <span className="tool-group-summary">
            {events.length} 个事件 · {toolCalls} 次调用 · {toolResults} 个结果
            {progressItems ? ` · ${progressItems} 个进度` : ""}
          </span>
          <span className="toggle-arrow">{expanded ? "收起" : "展开"}</span>
        </button>
        <div className="tool-collapsed-preview">最近: {latestLabel}</div>
        {expanded ? (
          <div className="tool-group-body">
            {events.map((evt, i) => {
              if (evt.type === "tool_call") {
                return <ToolCallBubble key={`tc-${i}`} toolName={evt.name} args={evt.arguments} status={statuses[i]?.state || "running"} />;
              }
              if (evt.type === "tool_result") {
                return <ToolResultBubble key={`tr-${i}`} toolName={evt.name} content={evt.content} state={statuses[i]?.state || "done"} />;
              }
              return <ProgressBubble key={`pg-${i}`} message={evt.message} elapsedSeconds={evt.elapsed_seconds} />;
            })}
          </div>
        ) : null}
      </div>
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
            {expanded ? "收起详情" : "展开详情"}
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
        <button className="sidebar-toggle" onClick={onToggle} title={open ? "折叠调试面板" : "展开调试面板"}>
          {open ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}
        </button>
        {open ? (
          <>
            <div>
              <div className="debug-sidebar-title">调试流</div>
              <div className="debug-sidebar-subtitle">{loading ? "实时事件" : "空闲"}</div>
            </div>
            <button className="clear-btn" onClick={onClear}>清空</button>
          </>
        ) : null}
      </div>
      {open ? (
        <div className="debug-sidebar-body">
          {events.length ? events.map((event, index) => <DebugEventCard key={`debug-${index}`} event={event} />) : (
            <div className="debug-empty">暂无调试事件。</div>
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
            {expanded ? "收起结果" : "展开结果"}
          </button>
          {expanded ? <pre className="tool-args">{task.result || task.error}</pre> : null}
        </>
      ) : null}
    </div>
  );
}

function TaskProgressPanel({ progress, onRefresh }) {
  const planTodos = progress?.task_plan?.todos || [];
  const tasks = planTodos.length ? planTodos : (progress?.task_items || []);
  const pitfalls = progress?.pitfalls || [];
  const stages = progress?.script_stages || [];

  return (
    <div className="task-progress-panel">
      <div className="task-progress-header">
        <div>
          <div className="debug-sidebar-title">任务进度</div>
          <div className="debug-sidebar-subtitle">
            {tasks.length} 项待办 / {pitfalls.length} 个坑点 / {stages.length} 个阶段
          </div>
        </div>
        <button className="session-toolbar-btn" onClick={onRefresh} title="刷新任务进度">
          <RefreshCw size={14} />
        </button>
      </div>
      <div className="task-progress-body">
        {tasks.length ? (
          <div className="progress-section">
            <div className="progress-section-title">{planTodos.length ? "任务计划" : "待办"}</div>
            {tasks.slice().reverse().map((task) => (
              <div className="progress-card" key={task.task_id || task.content}>
                <div className="progress-card-header">
                  <span>{task.status === "in_progress" ? (task.activeForm || task.content) : (task.content || task.title || task.task_id)}</span>
                  <span className={`progress-status progress-status-${task.status || "pending"}`}>
                    {task.status || "pending"}
                  </span>
                </div>
                {task.details ? <div className="progress-card-detail">{task.details}</div> : null}
                {task.result_ref || task.artifact_path ? <div className="progress-card-path">{task.result_ref || task.artifact_path}</div> : null}
              </div>
            ))}
          </div>
        ) : null}

        {pitfalls.length ? (
          <div className="progress-section">
            <div className="progress-section-title">坑点</div>
            {pitfalls.slice().reverse().map((pitfall, index) => (
              <div className="progress-card pitfall-card" key={`${pitfall.created_at || "pitfall"}-${index}`}>
                <div className="progress-card-header">
                  <span>{pitfall.summary || "Pitfall"}</span>
                </div>
                {pitfall.impact ? <div className="progress-card-detail">Impact: {pitfall.impact}</div> : null}
                {pitfall.resolution ? <div className="progress-card-detail">Resolution: {pitfall.resolution}</div> : null}
                {pitfall.artifact_path ? <div className="progress-card-path">{pitfall.artifact_path}</div> : null}
              </div>
            ))}
          </div>
        ) : null}

        {stages.length ? (
          <div className="progress-section">
            <div className="progress-section-title">脚本阶段</div>
            {stages.slice(-8).reverse().map((stage, index) => (
              <div className="progress-card" key={`${stage.created_at || "stage"}-${index}`}>
                <div className="progress-card-header">
                  <span>{stage.stage_name || "stage"}</span>
                  <span className={`progress-status progress-status-${stage.status || "pending"}`}>
                    {stage.status || "pending"}
                  </span>
                </div>
                {stage.summary ? <div className="progress-card-detail">{stage.summary}</div> : null}
                {stage.artifact_path ? <div className="progress-card-path">{stage.artifact_path}</div> : null}
              </div>
            ))}
          </div>
        ) : null}

        {!tasks.length && !pitfalls.length && !stages.length ? (
          <div className="debug-empty">暂无任务进度。</div>
        ) : null}
      </div>
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
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLowerCase();
  const filteredSessions = normalizedQuery
    ? sessions.filter((session) =>
        `${session.title || ""} ${session.session_id || ""}`.toLowerCase().includes(normalizedQuery)
      )
    : sessions;
  const groups = groupSessionsByDate(filteredSessions);

  return (
    <aside className={`session-sidebar ${collapsed ? "collapsed" : ""}`}>
      <div className="session-sidebar-header">
        <div className="session-brand">
          <Bot size={20} />
          {!collapsed ? <span>Chat Agent</span> : null}
        </div>
        <div className="session-sidebar-actions">
          <button className="session-create-btn" onClick={onCreate} title="新建会话">
            <Plus size={16} />
          </button>
          <button className="session-collapse-btn" onClick={onToggleCollapsed} title={collapsed ? "展开会话" : "折叠会话"}>
            {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
          </button>
        </div>
      </div>
      {!collapsed ? (
        <>
          <div className="session-search">
            <Search size={14} />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索会话"
              aria-label="搜索会话"
            />
          </div>
          <div className="session-sidebar-body">
            {groups.length ? (
              groups.map((group) => (
                <div key={group.label} className="session-group">
                  <div className="session-group-label">{group.label}</div>
                  {group.items.map((session) => (
                    <div
                      key={session.session_id}
                      className={`session-item ${session.session_id === activeSessionId ? "active" : ""}`}
                    >
                      <button className="session-item-main" onClick={() => onSelect(session.session_id)}>
                        <MessageSquare size={16} />
                        <div className="session-item-content">
                          <div className="session-item-title">{session.title || session.session_id}</div>
                          <div className="session-item-meta">
                            {session.task_progress?.status === "running" ? "运行中" : "空闲"}
                            {session.updated_at || session.created_at
                              ? ` · ${formatRelativeTime(session.updated_at || session.created_at)}`
                              : ""}
                          </div>
                        </div>
                      </button>
                      <div className="session-item-toolbar">
                        <button className="session-toolbar-btn" onClick={(e) => onRename(e, session)} title="重命名会话">
                          <Pencil size={14} />
                        </button>
                        <button className="session-toolbar-btn danger" onClick={(e) => onDelete(e, session)} title="删除会话">
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              ))
            ) : (
              <div className="session-empty">没有匹配的会话。</div>
            )}
          </div>
        </>
      ) : null}
    </aside>
  );
}

function FeishuLoginPanel({
  status,
  loginState,
  loading,
  polling,
  collapsed,
  onToggleCollapsed,
  onInit,
  onRefresh,
  onLogout,
}) {
  return (
    <section className={`feishu-panel ${collapsed ? "collapsed" : ""}`}>
      <div className="feishu-panel-header">
        <div className="feishu-panel-title">
          <QrCode size={16} />
          <span>飞书网页登录</span>
        </div>
        <div className="feishu-panel-header-right">
          <button
            className="feishu-collapse-btn"
            onClick={onToggleCollapsed}
            title={collapsed ? "显示飞书登录面板" : "隐藏飞书登录面板"}
            aria-label={collapsed ? "显示飞书登录面板" : "隐藏飞书登录面板"}
          >
            {collapsed ? <Expand size={14} /> : <Minimize size={14} />}
          </button>
          <div className={`feishu-status ${status?.logged_in ? "connected" : "disconnected"}`}>
            {status?.logged_in ? "已连接" : "未登录"}
          </div>
        </div>
      </div>
      {!collapsed ? (
        <>
          <div className="feishu-panel-actions">
            <button className="feishu-btn" onClick={onInit} disabled={loading || polling}>
              <LogIn size={14} />
              <span>{loading ? "加载中..." : polling ? "等待扫码..." : "生成二维码"}</span>
            </button>
            <button className="feishu-btn" onClick={onRefresh}>
              <RefreshCw size={14} />
              <span>状态</span>
            </button>
            <button className="feishu-btn danger" onClick={onLogout}>
              <LogOut size={14} />
              <span>退出登录</span>
            </button>
          </div>
          {loginState?.qr_png_base64 ? (
            <div className="feishu-qr-block">
              <img
                className="feishu-qr-image"
                src={`data:image/png;base64,${loginState.qr_png_base64}`}
                alt="飞书登录二维码"
              />
              <div className="feishu-qr-hint">在飞书中扫码,后端正在自动轮询。</div>
            </div>
          ) : null}
          {loginState?.message ? <div className="feishu-panel-note">{loginState.message}</div> : null}
          {status?.issued_at ? (
            <div className="feishu-panel-note">
              会话签发时间: {new Date(status.issued_at * 1000).toLocaleString()}
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function ColumnResizer({ label, onPointerDown, onStep }) {
  const handleKeyDown = (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    onStep(event.key === "ArrowRight" ? 24 : -24);
  };

  return (
    <div
      className="column-resizer"
      role="separator"
      aria-label={label}
      aria-orientation="vertical"
      tabIndex={0}
      onPointerDown={onPointerDown}
      onKeyDown={handleKeyDown}
    />
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
        <span className="diagram-title">{title || "架构图"}</span>
        <div className="diagram-actions">
          <button
            className="diagram-action-btn"
            onClick={() => setExpanded((v) => !v)}
            title={expanded ? "收起" : "展开"}
          >
            {expanded ? <Minimize size={14} /> : <Expand size={14} />}
          </button>
          <a
            className="diagram-action-btn"
            href={url}
            download="architecture-diagram.html"
            title="下载 HTML"
          >
            <Download size={14} />
          </a>
        </div>
      </div>
      <div className="diagram-frame-wrapper">
        <iframe
          className="diagram-frame"
          src={url}
          title={title || "架构图"}
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
            <span>技能</span>
          </div>
          <button className="skill-popup-close" onClick={onClose} title="关闭">
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
            已安装 ({installedSkills.length})
          </button>
          <button
            className={`skill-tab ${skillTab === "available" ? "active" : ""}`}
            onClick={() => onTabChange("available")}
          >
            <PlusCircle size={14} />
            可用 ({availableSkills.length})
          </button>
        </div>

        {/* Body */}
        <div className="skill-popup-body">
          {skillTab === "installed" && (
            <div className="skill-list">
              {installedSkills.length === 0 ? (
                <div className="skill-empty">暂无已安装技能。切换到「可用」页签安装。</div>
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
                      title="卸载"
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
                <div className="skill-empty">注册表中暂无其他可用技能。</div>
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
                      title="安装"
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
                <span>执行: <strong>{selectedSkill.name}</strong></span>
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
                {skillLoading ? "执行中..." : "执行"}
              </button>

              {/* Result display — show rendered diagram if HTML, otherwise show text */}
              {skillResult && (
                <div className={`skill-result ${skillResult.type}`}>
                  <div className="skill-result-header">
                    {skillResult.type === "success" ? (
                      <><Sparkles size={14} /> 结果</>
                    ) : (
                      <><X size={14} /> 错误</>
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
function nodeToText(node) {
  if (node == null) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeToText).join("");
  if (node?.props?.children !== undefined) return nodeToText(node.props.children);
  return "";
}

function CodeBlock({ children }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    const text = nodeToText(children);
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(text);
      setCopied(true);
      showToast("代码已复制", "success");
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      showToast("代码复制失败", "error");
    }
  };

  return (
    <div className="code-block">
      <button
        type="button"
        className="code-copy-btn"
        onClick={handleCopy}
        aria-label="复制代码"
        title="复制代码"
      >
        {copied ? <Check size={13} /> : <Copy size={13} />}
      </button>
      <pre>{children}</pre>
    </div>
  );
}

const MARKDOWN_COMPONENTS = { pre: CodeBlock };

export function ChatMessageContent({ content }) {
  const htmlContent = useMemo(() => extractHtmlContent(content), [content]);

  // If the content is primarily an HTML diagram, render the diagram and the text
  if (htmlContent) {
    // Get text before and after the HTML block
    const parts = content.split(/```html\s*\n?[\s\S]*?```/i);
    const textBefore = parts[0]?.trim();
    const textAfter = parts[1]?.trim();

    return (
      <div className="message-content">
        {textBefore && (
          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]} components={MARKDOWN_COMPONENTS}>
            {textBefore}
          </ReactMarkdown>
        )}
        <DiagramIframe htmlContent={htmlContent} title="架构图" />
        {textAfter && (
          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]} components={MARKDOWN_COMPONENTS}>
            {textAfter}
          </ReactMarkdown>
        )}
      </div>
    );
  }

  return (
    <div className="message-content">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]} components={MARKDOWN_COMPONENTS}>
        {content}
      </ReactMarkdown>
    </div>
  );
}

function parseJsonLike(value) {
  if (value && typeof value === "object") return value;
  if (typeof value !== "string") return null;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function extractKnowledgeCitations(tools = []) {
  const citations = [];
  for (const item of tools || []) {
    if ((item?.name || "").toLowerCase() !== "knowledge_search") continue;
    const payload = parseJsonLike(item.content);
    const results = Array.isArray(payload?.results) ? payload.results : [];
    for (const result of results) {
      citations.push({
        id: result.citation_id || result.chunk_id || result.doc_id || "knowledge-source",
        title: result.title || result.source_ref || result.source_uri || "Knowledge source",
        collection: result.collection || "",
        snippet: result.snippet || "",
        heading: Array.isArray(result.heading_path) ? result.heading_path.join(" / ") : "",
      });
    }
  }
  return citations;
}

function KnowledgeCitations({ tools }) {
  const citations = extractKnowledgeCitations(tools);
  if (!citations.length) return null;
  return (
    <div className="knowledge-citations">
      <div className="knowledge-citations-title">知识引用</div>
      {citations.map((citation, index) => (
        <div className="knowledge-citation" key={`${citation.id}-${index}`}>
          <div className="knowledge-citation-source">
            <FileText size={13} />
            <span>{citation.title}</span>
            <code>{citation.id}</code>
          </div>
          <div className="knowledge-citation-meta">
            {[citation.collection, citation.heading].filter(Boolean).join(" / ")}
          </div>
          {citation.snippet ? <div className="knowledge-citation-snippet">{citation.snippet}</div> : null}
        </div>
      ))}
    </div>
  );
}

const RESEARCH_OUTCOME_LABELS = {
  answer_ready: "回答就绪",
  evidence_gap: "证据缺口",
  failed: "受阻",
};

const RESEARCH_SOURCE_LABELS = {
  web: "网络",
  web_search: "网络检索",
  web_fetch: "网页读取",
  personal_knowledge: "个人知识库",
  workspace: "工作区",
  feishu_docs: "飞书文档",
};

function researchOutcomeLabel(value) {
  const raw = String(value || "evidence_gap");
  return RESEARCH_OUTCOME_LABELS[raw] || raw.replaceAll("_", " ");
}

function researchSourceLabel(value) {
  const raw = String(value || "");
  return RESEARCH_SOURCE_LABELS[raw] || raw.replaceAll("_", " ");
}

function ResearchProvenance({ research }) {
  if (!research || typeof research !== "object") return null;
  const routeClass = String(research.route_class || "").trim();
  const outcomeRaw = String(research.outcome || routeClass || "evidence_gap");
  const outcome = researchOutcomeLabel(outcomeRaw);
  const sources = Array.isArray(research.sources_attempted) ? research.sources_attempted : [];
  const attempts = research.attempts && typeof research.attempts === "object" ? research.attempts : {};
  const budget = research.budget && typeof research.budget === "object" ? research.budget : {};
  const citationCounts = research.citation_counts && typeof research.citation_counts === "object"
    ? research.citation_counts
    : {};
  const citationTotal = Object.values(citationCounts).reduce(
    (total, value) => total + (typeof value === "number" ? value : 0),
    0
  );
  return (
    <section className={`research-provenance research-${outcomeRaw.replace(/[^a-z0-9]+/gi, "-")}`} aria-label="研究溯源">
      <div className="research-provenance-header">
        <Search size={13} />
        <span>研究溯源</span>
        <strong>{outcome}</strong>
      </div>
      {sources.length ? (
        <div className="research-source-list">
          {sources.map((source) => (
            <span className="research-source-tag" key={source}>
              {researchSourceLabel(source)}
              {attempts[source] != null ? ` · ${attempts[source]}` : ""}
            </span>
          ))}
        </div>
      ) : null}
      <div className="research-provenance-meta">
        <span>{citationTotal} 条引用</span>
        {budget.route_transitions_limit != null ? (
          <span>路由 {budget.route_transitions_used || 0}/{budget.route_transitions_limit}</span>
        ) : null}
      </div>
      {outcomeRaw !== "answer_ready" || routeClass === "failed" ? (
        <div className="research-provenance-limit">
          <AlertCircle size={13} />
          <span>可用证据不足以形成完全有据的回答。</span>
        </div>
      ) : null}
    </section>
  );
}

const KNOWLEDGE_COUNT_LABELS = {
  indexed: "已索引",
  refreshed: "已刷新",
  unchanged: "无变化",
  skipped: "已跳过",
  failed: "失败",
  deleted: "已删除",
};

function formatKnowledgeCounts(status) {
  const counts = status?.counts || {};
  const parts = Object.keys(KNOWLEDGE_COUNT_LABELS)
    .filter((key) => counts[key])
    .map((key) => `${KNOWLEDGE_COUNT_LABELS[key]} ${counts[key]}`);
  if (parts.length) return parts.join(" / ");
  if (status?.message) return status.message;
  return "就绪";
}

function knowledgeFileName(value) {
  const raw = String(value || "");
  return raw.split(/[\\/]/).filter(Boolean).pop() || raw || "document";
}

function knowledgeStatusClass(status) {
  return `knowledge-status-${String(status || "unknown").replace(/[^a-z0-9_-]+/gi, "_").toLowerCase()}`;
}

const REMOTE_KNOWLEDGE_ERROR_MESSAGES = {
  invalid_reference: "文档链接不是受支持的链接或 Token。",
  content_invalid: "文档内容为空、过大或无法规范化。",
  auth_required: "飞书会话缺失或已过期,请重新登录后再导入。",
  permission_denied: "当前账号无权访问该文档。",
  not_found: "未找到该文档。",
  rate_limited: "远程服务正在限流,请稍后再试。",
  provider_unavailable: "远程文档服务暂时不可用。",
};

const KNOWLEDGE_STATUS_LABELS = {
  indexed: "已索引",
  refreshed: "已刷新",
  unchanged: "无变化",
};

function remoteKnowledgeStatusMessage(payload) {
  const status = String(payload?.status || "").toLowerCase();
  const title = payload?.title || payload?.source_uri || "文档";
  if (KNOWLEDGE_STATUS_LABELS[status]) {
    return `${KNOWLEDGE_STATUS_LABELS[status]} / ${title}`;
  }
  return payload?.message || "知识库操作完成。";
}

function remoteKnowledgeErrorMessage(payload, httpStatus) {
  const code = String(payload?.error?.code || "").toLowerCase();
  return REMOTE_KNOWLEDGE_ERROR_MESSAGES[code]
    || payload?.error?.message
    || `知识库导入失败${httpStatus ? `(HTTP ${httpStatus})` : ""}。`;
}

function knowledgeSyncDetails(status) {
  return Array.isArray(status?.files)
    ? status.files.filter((item) => ["failed", "skipped", "unsupported"].includes(String(item.status || "").toLowerCase()))
    : [];
}

function KnowledgeSyncDetails({ status }) {
  const details = knowledgeSyncDetails(status);
  if (!details.length) return null;
  return (
    <div className="knowledge-sync-details" aria-label="Knowledge sync details">
      {details.slice(0, 4).map((item, index) => (
        <div className="knowledge-sync-detail" key={`${item.path || item.source_uri || index}`}>
          <span className={`knowledge-status-pill ${knowledgeStatusClass(item.status)}`}>{item.status || "issue"}</span>
          <span className="knowledge-sync-path">{knowledgeFileName(item.path || item.source_uri)}</span>
          {item.reason ? <span className="knowledge-sync-reason">{item.reason}</span> : null}
        </div>
      ))}
    </div>
  );
}

function FeishuImportDialog({
  open,
  loggedIn,
  loading,
  onClose,
  onSubmit,
  onOpenLogin,
}) {
  const [reference, setReference] = useState("");
  const [collection, setCollection] = useState("default");
  const [refresh, setRefresh] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setReference("");
    setCollection("default");
    setRefresh(false);
    setError("");
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const handleKeyDown = (event) => {
      if (event.key === "Escape" && !loading) onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [loading, onClose, open]);

  if (!open) return null;

  const handleSubmit = async (event) => {
    event.preventDefault();
    const nextReference = reference.trim();
    const nextCollection = collection.trim();
    if (!nextReference) {
      setError("请输入文档链接或 Token。");
      return;
    }
    if (!nextCollection) {
      setError("请输入知识集合。");
      return;
    }
    if (!loggedIn) {
      setError("Sign in to Feishu before importing a document.");
      return;
    }

    setError("");
    try {
      const result = await onSubmit({
        reference: nextReference,
        collection: nextCollection,
        refresh,
      });
      if (result?.ok) onClose();
      else setError(result?.message || "文档导入失败。");
    } catch (submitError) {
      setError(submitError?.message || "文档导入失败。");
    }
  };

  const handleOpenLogin = () => {
    onClose();
    onOpenLogin();
  };

  return (
    <div
      className="feishu-import-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="导入飞书文档"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !loading) onClose();
      }}
    >
      <section className="feishu-import-dialog">
        <header className="feishu-import-header">
          <div className="feishu-import-title">
            <Link2 size={17} />
            <span>导入飞书文档</span>
          </div>
          <button
            type="button"
            className="feishu-import-close"
            onClick={onClose}
            disabled={loading}
            aria-label="Close Feishu document import"
          >
            <X size={16} />
          </button>
        </header>

        <form className="feishu-import-form" onSubmit={handleSubmit}>
          <label className="feishu-import-field">
            <span>飞书文档链接或 Token</span>
            <input
              value={reference}
              onChange={(event) => setReference(event.target.value)}
              placeholder="粘贴文档链接或 Token"
              autoFocus
              disabled={loading}
            />
          </label>
          <label className="feishu-import-field">
            <span>知识集合</span>
            <input
              value={collection}
              onChange={(event) => setCollection(event.target.value)}
              placeholder="default"
              disabled={loading}
            />
          </label>
          <label className="feishu-import-checkbox">
            <input
              type="checkbox"
              checked={refresh}
              onChange={(event) => setRefresh(event.target.checked)}
              disabled={loading}
            />
            <span>强制刷新远程内容</span>
          </label>

          {!loggedIn ? (
            <div className="feishu-import-alert" role="status">
              <AlertCircle size={15} />
              <span>导入前请先登录飞书。</span>
              <button type="button" className="feishu-import-login" onClick={handleOpenLogin} disabled={loading}>
                打开登录面板
              </button>
            </div>
          ) : null}
          {error ? (
            <div className="feishu-import-error" role="alert">
              <AlertCircle size={15} />
              <span>{error}</span>
            </div>
          ) : null}

          <footer className="feishu-import-footer">
            <button type="button" className="feishu-import-cancel" onClick={onClose} disabled={loading}>
              取消
            </button>
            <button
              type="submit"
              className="feishu-import-submit"
              disabled={loading || !loggedIn || !reference.trim() || !collection.trim()}
            >
              {loading ? <Loader2 className="spin" size={14} /> : <Link2 size={14} />}
              加入知识库
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function KnowledgeCenter({
  open,
  documents,
  sources,
  detail,
  detailLoading,
  loading,
  onClose,
  onInspect,
  onRefresh,
  onDeleteDocument,
}) {
  const [query, setQuery] = useState("");
  const [collection, setCollection] = useState("all");
  if (!open) return null;

  const sourceRows = sources?.length
    ? sources
    : (documents || []).map((doc) => ({
        ...doc,
        title: doc.title || knowledgeFileName(doc.source_uri),
        status: doc.status || "indexed",
      }));
  const collections = Array.from(
    new Set(sourceRows.map((item) => item.collection).filter(Boolean))
  ).sort((a, b) => a.localeCompare(b));
  const normalizedQuery = query.trim().toLowerCase();
  const visibleRows = sourceRows.filter((item) => {
    const matchesCollection = collection === "all" || item.collection === collection;
    const haystack = [item.title, item.source_uri, item.status, item.reason, item.collection]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return matchesCollection && (!normalizedQuery || haystack.includes(normalizedQuery));
  });
  const selectedDocument = detail?.document || null;

  return (
    <div className="knowledge-center-overlay" role="dialog" aria-modal="true" aria-label="知识中心">
      <section className="knowledge-center">
        <header className="knowledge-center-header">
          <div>
            <div className="knowledge-center-title">知识中心</div>
            <div className="knowledge-center-subtitle">{sourceRows.length} 个源文件 / {documents.length} 个已索引文档</div>
          </div>
          <div className="knowledge-center-actions">
            <button type="button" className="knowledge-action-btn" onClick={onRefresh} disabled={loading}>
              {loading ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />}
              刷新
            </button>
            <button type="button" className="knowledge-center-close" onClick={onClose} aria-label="关闭知识中心">
              <X size={17} />
            </button>
          </div>
        </header>

        <div className="knowledge-center-toolbar">
          <div className="knowledge-search">
            <Search size={15} />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索知识文件"
              aria-label="搜索知识文件"
            />
          </div>
        </div>

        <div className="knowledge-center-body">
          <aside className="knowledge-center-sidebar" aria-label="知识集合">
            <button
              type="button"
              className={`knowledge-collection-btn ${collection === "all" ? "active" : ""}`}
              onClick={() => setCollection("all")}
            >
              全部
            </button>
            {collections.map((item) => (
              <button
                type="button"
                className={`knowledge-collection-btn ${collection === item ? "active" : ""}`}
                onClick={() => setCollection(item)}
                key={item}
              >
                {item}
              </button>
            ))}
          </aside>

          <section className="knowledge-source-list" aria-label="知识源文件">
            <div className="knowledge-source-header">
              <span>文件</span>
              <span>状态</span>
              <span>分块</span>
            </div>
            {visibleRows.length ? (
              visibleRows.map((item) => {
                const title = item.title || knowledgeFileName(item.source_uri);
                return (
                  <div className="knowledge-source-row" key={`${item.source_uri || item.doc_id}-${title}`}>
                    <button
                      type="button"
                      className="knowledge-source-title"
                      onClick={() => onInspect(item)}
                      aria-label={`查看 ${title}`}
                      disabled={!item.doc_id}
                    >
                      <FileText size={15} />
                      <span>{title}</span>
                    </button>
                    <span className={`knowledge-status-pill ${knowledgeStatusClass(item.status)}`}>{item.status || "unknown"}</span>
                    <span className="knowledge-source-chunks">{item.chunk_count || 0}</span>
                    <div className="knowledge-source-meta">
                      {[item.collection ? `collection ${item.collection}` : "", item.reason].filter(Boolean).join(" / ")}
                    </div>
                    {item.doc_id || item.source_uri ? (
                      <button
                        type="button"
                        className="knowledge-source-delete"
                        onClick={() => onDeleteDocument(item)}
                        aria-label={`删除 ${title}`}
                        disabled={loading}
                      >
                        <Trash2 size={13} />
                      </button>
                    ) : null}
                  </div>
                );
              })
            ) : (
              <div className="knowledge-center-empty">没有匹配的知识文件。</div>
            )}
          </section>

          <aside className="knowledge-detail-panel" aria-label="知识文档详情">
            {detailLoading ? (
              <div className="knowledge-center-empty"><Loader2 className="spin" size={16} /> 正在加载文档...</div>
            ) : selectedDocument ? (
              <>
                <div className="knowledge-detail-title">{selectedDocument.title || selectedDocument.doc_id}</div>
                <div className="knowledge-detail-meta">
                  {[selectedDocument.collection, selectedDocument.source_type, `${selectedDocument.chunk_count || 0} chunks`]
                    .filter(Boolean)
                    .join(" / ")}
                </div>
                <div className="knowledge-detail-path">{selectedDocument.source_uri}</div>
                <div className="knowledge-chunk-list">
                  {(detail?.chunks || []).map((chunk) => (
                    <div className="knowledge-chunk-card" key={chunk.chunk_id}>
                      <div className="knowledge-chunk-header">
                        <code>{chunk.citation_id || chunk.chunk_id}</code>
                        <span>{chunk.heading_path?.length ? chunk.heading_path.join(" / ") : `chunk ${chunk.ordinal ?? ""}`}</span>
                      </div>
                      <pre>{chunk.text}</pre>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <div className="knowledge-center-empty">选择一个文档查看分块。</div>
            )}
          </aside>
        </div>
      </section>
    </div>
  );
}

function KnowledgePanel({
  documents,
  knowledgePolicy,
  loading,
  status,
  feishuStatus,
  collapsed,
  onKnowledgePolicyChange,
  onToggleCollapsed,
  onImportFiles,
  onImportFeishu,
  onOpenFeishuLogin,
  onSync,
  onDeleteDocument,
  onOpenCenter,
}) {
  const importInputRef = useRef(null);
  const [feishuImportOpen, setFeishuImportOpen] = useState(false);
  const handleDrop = (event) => {
    if (!event.dataTransfer.files.length) return;
    event.preventDefault();
    onImportFiles(event.dataTransfer.files);
  };
  const handleDragOver = (event) => {
    if (!event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  };

  return (
    <section
      className={`knowledge-panel ${collapsed ? "collapsed" : ""}`}
      aria-label="知识库"
      onDrop={handleDrop}
      onDragOver={handleDragOver}
    >
      <div className="knowledge-panel-header">
        <div>
          <div className="knowledge-panel-title">知识库</div>
          <div className="knowledge-panel-subtitle">{documents.length} 个文档 / {formatKnowledgeCounts(status)}</div>
        </div>
        <div className="knowledge-panel-header-right">
          <div className="knowledge-policy-control" role="radiogroup" aria-label="知识检索策略">
            {[["auto", "自动"], ["required", "必须"], ["disabled", "关闭"]].map(([policy, policyLabel]) => (
              <label className={`knowledge-policy-option ${knowledgePolicy === policy ? "active" : ""}`} key={policy}>
                <input
                  type="radio"
                  name="knowledge-policy"
                  value={policy}
                  checked={knowledgePolicy === policy}
                  onChange={() => onKnowledgePolicyChange(policy)}
                  disabled={loading}
                />
                <span>{policyLabel}</span>
              </label>
            ))}
          </div>
          <button
            type="button"
            className="knowledge-collapse-btn"
            onClick={onToggleCollapsed}
            title={collapsed ? "显示知识库面板" : "隐藏知识库面板"}
            aria-label={collapsed ? "显示知识库面板" : "隐藏知识库面板"}
          >
            {collapsed ? <Expand size={14} /> : <Minimize size={14} />}
          </button>
        </div>
      </div>
      {!collapsed ? (
        <>
          <div className="knowledge-panel-actions">
            <button
              type="button"
              className="knowledge-action-btn"
              onClick={() => importInputRef.current?.click()}
              disabled={loading}
            >
              <Paperclip size={14} />
              导入
            </button>
            <input
              ref={importInputRef}
              className="file-input"
              type="file"
              multiple
              aria-label="导入知识文件"
              onChange={(event) => {
                onImportFiles(event.target.files);
                event.target.value = "";
              }}
              disabled={loading}
            />
            <button
              type="button"
              className="knowledge-action-btn"
              onClick={() => setFeishuImportOpen(true)}
              disabled={loading}
              aria-label="导入飞书文档"
              title="导入飞书文档"
            >
              <Link2 size={14} />
              飞书文档
            </button>
            <button
              type="button"
              className="knowledge-action-btn"
              onClick={onOpenCenter}
              disabled={loading}
              aria-label="打开知识中心"
            >
              <FileText size={14} />
              浏览
            </button>
            <button
              type="button"
              className="knowledge-action-btn"
              onClick={onSync}
              disabled={loading}
              aria-label="同步知识库"
            >
              {loading ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />}
              同步
            </button>
          </div>
          <div className="knowledge-documents">
            {documents.length ? (
              <div className="knowledge-empty">{documents.length} 个文档已索引。打开知识中心查看文件与分块。</div>
            ) : (
              <div className="knowledge-empty">拖入文件或导入文档。</div>
            )}
          </div>
          <KnowledgeSyncDetails status={status} />
        </>
      ) : null}
      <FeishuImportDialog
        open={feishuImportOpen}
        loggedIn={Boolean(feishuStatus?.logged_in)}
        loading={loading}
        onClose={() => setFeishuImportOpen(false)}
        onSubmit={onImportFeishu}
        onOpenLogin={onOpenFeishuLogin}
      />
    </section>
  );
}

function ThemeToggle({ value, onChange }) {
  const labels = { light: "浅色", dark: "暗色", system: "跟随系统" };
  return (
    <div className="theme-toggle" role="radiogroup" aria-label="主题">
      {THEME_OPTIONS.map((option) => (
        <label key={option} className={`theme-toggle-option ${value === option ? "active" : ""}`}>
          <input
            type="radio"
            name="theme-preference"
            value={option}
            checked={value === option}
            onChange={() => onChange(option)}
          />
          <span>{labels[option]}</span>
        </label>
      ))}
    </div>
  );
}

export default function App() {
  const [sessions, setSessions] = useState([]);
  const [activeSessionId, setActiveSessionId] = useState("");
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [pendingFiles, setPendingFiles] = useState([]);
  const [isDraggingFiles, setIsDraggingFiles] = useState(false);
  const [isDraggingImages, setIsDraggingImages] = useState(false);
  const [attachmentError, setAttachmentError] = useState("");
  const [knowledgePolicy, setKnowledgePolicy] = useState("auto");
  const [knowledgeDocuments, setKnowledgeDocuments] = useState([]);
  const [knowledgeSources, setKnowledgeSources] = useState([]);
  const [knowledgeStatus, setKnowledgeStatus] = useState({ message: "就绪" });
  const [knowledgeLoading, setKnowledgeLoading] = useState(false);
  const [knowledgeCenterOpen, setKnowledgeCenterOpen] = useState(false);
  const [knowledgeDetail, setKnowledgeDetail] = useState(null);
  const [knowledgeDetailLoading, setKnowledgeDetailLoading] = useState(false);
  const [sessionRuns, setSessionRuns] = useState({});
  const [subagentTasks, setSubagentTasks] = useState([]);
  const [subagentNotifications, setSubagentNotifications] = useState([]);
  const [taskProgress, setTaskProgress] = useState(null);
  const [debugOpen, setDebugOpen] = useState(() => initialDebugSidebarOpen(window.innerWidth, readDebugCollapsed()));
  const [sessionSidebarCollapsed, setSessionSidebarCollapsed] = useState(readSidebarCollapsed);
  const [knowledgePanelCollapsed, setKnowledgePanelCollapsed] = useState(readKnowledgeCollapsed);
  const [sessionSidebarWidth, setSessionSidebarWidth] = useState(() =>
    readStoredWidth(SESSION_SIDEBAR_WIDTH_KEY, SESSION_SIDEBAR_DEFAULT_WIDTH, SESSION_SIDEBAR_MIN_WIDTH, SESSION_SIDEBAR_MAX_WIDTH)
  );
  const [debugSidebarWidth, setDebugSidebarWidth] = useState(() =>
    readStoredWidth(DEBUG_SIDEBAR_WIDTH_KEY, DEBUG_SIDEBAR_DEFAULT_WIDTH, DEBUG_SIDEBAR_MIN_WIDTH, DEBUG_SIDEBAR_MAX_WIDTH)
  );
  const [subagentSidebarWidth, setSubagentSidebarWidth] = useState(() =>
    readStoredWidth(SUBAGENT_SIDEBAR_WIDTH_KEY, SUBAGENT_SIDEBAR_DEFAULT_WIDTH, SUBAGENT_SIDEBAR_MIN_WIDTH, SUBAGENT_SIDEBAR_MAX_WIDTH)
  );
  const [shouldAutoScroll, setShouldAutoScroll] = useState(true);
  const chatAreaRef = useRef(null);
  const chatEndRef = useRef(null);
  const abortControllersRef = useRef(new Map());
  const activeSessionIdRef = useRef("");
  const textareaRef = useRef(null);
  const fileInputRef = useRef(null);
  const dragDepthRef = useRef(0);

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
  const [feishuPanelCollapsed, setFeishuPanelCollapsed] = useState(readFeishuCollapsed);
  const [contextStats, setContextStats] = useState(null);
  const [messageFeedback, setMessageFeedback] = useState(readFeedbackStore);
  const [welcomeConfig, setWelcomeConfig] = useState(null);
  const [renameDialogSession, setRenameDialogSession] = useState(null);
  const [deleteDialogSession, setDeleteDialogSession] = useState(null);
  const [sessionDialogBusy, setSessionDialogBusy] = useState(false);
  const [themePreference, setThemePreferenceState] = useState(readThemePreference);
  const activeRun = getSessionRun(sessionRuns, activeSessionId);
  const loading = isSessionRunning(sessionRuns, activeSessionId);
  const submitMode = getSubmitMode(sessionRuns, activeSessionId);
  const appendMode = submitMode === "append";
  const streamingText = activeRun.streamingText || "";
  const toolEvents = activeRun.toolEvents || [];
  const activityItems = activeRun.activityItems || [];
  const debugEvents = activeRun.debugEvents || [];

  const scrollDown = useCallback((behavior = "auto") => {
    chatEndRef.current?.scrollIntoView({ behavior });
  }, []);

  const handleChatScroll = useCallback(() => {
    const node = chatAreaRef.current;
    if (!node) return;
    const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
    setShouldAutoScroll(nearBottom);
  }, []);

  const resizeComposer = useCallback(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = "auto";
    const nextHeight = Math.min(node.scrollHeight, COMPOSER_MAX_HEIGHT);
    node.style.height = `${Math.max(nextHeight, 36)}px`;
    node.style.overflowY = node.scrollHeight > COMPOSER_MAX_HEIGHT ? "auto" : "hidden";
  }, []);

  useEffect(() => {
    resizeComposer();
  }, [input, resizeComposer]);

  const addPendingFiles = useCallback((fileList) => {
    const incoming = Array.from(fileList || []);
    if (!incoming.length) return;
    setAttachmentError("");
    setPendingFiles((current) => {
      const next = [...current];
      for (const file of incoming) {
        if (file.size > MAX_ATTACHMENT_BYTES) {
          setAttachmentError(`文件 ${file.name} 超过 25 MB 限制。`);
          continue;
        }
        const duplicate = next.some(
          (item) =>
            item.name === file.name &&
            item.size === file.size &&
            item.lastModified === file.lastModified
        );
        if (!duplicate && next.length < MAX_ATTACHMENTS) next.push(file);
      }
      if (incoming.length + current.length > MAX_ATTACHMENTS) {
        setAttachmentError(`最多附加 ${MAX_ATTACHMENTS} 个文件。`);
      }
      return next;
    });
  }, []);

  const removePendingFile = useCallback((index) => {
    setPendingFiles((current) => current.filter((_, itemIndex) => itemIndex !== index));
    setAttachmentError("");
  }, []);

  const uploadPendingFiles = useCallback(async (sessionId, files, signal) => {
    if (!files.length) return [];
    const formData = new FormData();
    formData.append("session_id", sessionId);
    files.forEach((file) => formData.append("files", file, file.name));
    const response = await fetch(`${API_BASE}/uploads`, {
      method: "POST",
      body: formData,
      signal,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || `上传失败 (HTTP ${response.status})`);
    }
    return payload.files || [];
  }, []);

  const refreshKnowledgeDocuments = useCallback(async () => {
    try {
      const resp = await fetch(`${API_BASE}/knowledge/documents`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const payload = await resp.json();
      setKnowledgeDocuments(Array.isArray(payload.documents) ? payload.documents : []);
      return payload.documents || [];
    } catch {
      return [];
    }
  }, []);

  const refreshKnowledgeSources = useCallback(async () => {
    try {
      const resp = await fetch(`${API_BASE}/knowledge/sources`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const payload = await resp.json();
      setKnowledgeSources(Array.isArray(payload.sources) ? payload.sources : []);
      return payload.sources || [];
    } catch {
      setKnowledgeSources([]);
      return [];
    }
  }, []);

  const refreshKnowledgeCenter = useCallback(async () => {
    const [documents, sources] = await Promise.all([
      refreshKnowledgeDocuments(),
      refreshKnowledgeSources(),
    ]);
    return { documents, sources };
  }, [refreshKnowledgeDocuments, refreshKnowledgeSources]);

  const importKnowledgeFiles = useCallback(async (fileList) => {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const formData = new FormData();
    formData.append("collection", "default");
    files.forEach((file) => formData.append("files", file, file.name));
    setKnowledgeLoading(true);
    try {
      const resp = await fetch(`${API_BASE}/knowledge/import`, {
        method: "POST",
        body: formData,
      });
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(payload.error || `导入失败 (HTTP ${resp.status})`);
      setKnowledgeStatus(payload);
      showToast(`已导入 ${files.length} 个文件到知识库`, "success");
      await refreshKnowledgeCenter();
    } catch (err) {
      setKnowledgeStatus({ message: err.message || "import failed", counts: { failed: files.length } });
      showToast(err.message || "知识库导入失败", "error");
    } finally {
      setKnowledgeLoading(false);
    }
  }, [refreshKnowledgeCenter]);

  const syncKnowledge = useCallback(async () => {
    setKnowledgeLoading(true);
    try {
      const resp = await fetch(`${API_BASE}/knowledge/sync`, {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({ collection: null }),
      });
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(payload.error || `同步失败 (HTTP ${resp.status})`);
      setKnowledgeStatus(payload);
      showToast("知识库同步完成", "success");
      await refreshKnowledgeCenter();
    } catch (err) {
      setKnowledgeStatus({ message: err.message || "sync failed", counts: { failed: 1 } });
      showToast(err.message || "知识库同步失败", "error");
    } finally {
      setKnowledgeLoading(false);
    }
  }, [refreshKnowledgeCenter]);

  const deleteKnowledgeDocument = useCallback(async (doc) => {
    if (!doc?.doc_id && !doc?.source_uri) return;
    setKnowledgeLoading(true);
    try {
      const deleteUrl = doc.source_uri
        ? `${API_BASE}/knowledge/sources?source_uri=${encodeURIComponent(doc.source_uri)}`
        : `${API_BASE}/knowledge/documents/${encodeURIComponent(doc.doc_id)}?remove_source=true`;
      const resp = await fetch(deleteUrl, {
        method: "DELETE",
      });
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(payload.error || `删除失败 (HTTP ${resp.status})`);
      setKnowledgeStatus({ ...payload, message: "deleted" });
      showToast("已删除知识文档", "success");
      setKnowledgeDetail((current) => (
        current?.document?.doc_id === doc.doc_id || current?.document?.source_uri === doc.source_uri ? null : current
      ));
      await refreshKnowledgeCenter();
    } catch (err) {
      setKnowledgeStatus({ message: err.message || "delete failed", counts: { failed: 1 } });
      showToast(err.message || "知识文档删除失败", "error");
    } finally {
      setKnowledgeLoading(false);
    }
  }, [refreshKnowledgeCenter]);

  const inspectKnowledgeDocument = useCallback(async (doc) => {
    if (!doc?.doc_id) return;
    setKnowledgeCenterOpen(true);
    setKnowledgeDetailLoading(true);
    try {
      const resp = await fetch(`${API_BASE}/knowledge/documents/${encodeURIComponent(doc.doc_id)}`);
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(payload.error || `文档加载失败 (HTTP ${resp.status})`);
      setKnowledgeDetail(payload);
    } catch (err) {
      setKnowledgeDetail({
        document: {
          doc_id: doc.doc_id,
          title: doc.title || knowledgeFileName(doc.source_uri),
          source_uri: doc.source_uri || "",
          collection: doc.collection || "",
          source_type: doc.source_type || "",
          chunk_count: 0,
          latest_error: err.message || "document load failed",
        },
        chunks: [],
      });
    } finally {
      setKnowledgeDetailLoading(false);
    }
  }, []);

  const handleDragEnter = useCallback((event) => {
    if (!event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    dragDepthRef.current += 1;
    setIsDraggingFiles(true);
    setIsDraggingImages(dataTransferHasImage(event.dataTransfer));
  }, []);

  const handleDragOver = useCallback((event) => {
    if (!event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setIsDraggingImages(dataTransferHasImage(event.dataTransfer));
  }, []);

  const handleDragLeave = useCallback((event) => {
    event.preventDefault();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) {
      setIsDraggingFiles(false);
      setIsDraggingImages(false);
    }
  }, []);

  const handleDrop = useCallback(
    (event) => {
      if (!event.dataTransfer.files.length) return;
      event.preventDefault();
      dragDepthRef.current = 0;
      setIsDraggingFiles(false);
      setIsDraggingImages(false);
      addPendingFiles(event.dataTransfer.files);
    },
    [addPendingFiles]
  );

  const handlePaste = useCallback(
    (event) => {
      const imageFiles = imageFilesFromClipboard(event.clipboardData);
      if (!imageFiles.length) return;
      addPendingFiles(imageFiles);
    },
    [addPendingFiles]
  );

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
      if (data.logged_in) {
        setFeishuLoginState((prev) => prev?.qr_png_base64 ? { message: "Login session is active." } : prev);
      } else if (!feishuPolling) {
        setFeishuLoginState((prev) => (prev?.session ? null : prev));
      }
      return data;
    } catch {
      const fallback = { logged_in: false, has_session: false, issued_at: null, metadata: {} };
      setFeishuStatus(fallback);
      return fallback;
    }
  }, [feishuPolling]);

  const loadSession = useCallback(async (sessionId, options = {}) => {
    const { scrollToBottom = true } = options;
    try {
      const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setActiveSessionId(data.session_id);
      activeSessionIdRef.current = data.session_id;
      writeLastSessionId(data.session_id);
      setMessages(data.messages || []);
      setSubagentTasks(data.subagent_tasks || []);
      setSubagentNotifications(data.subagent_notifications || []);
      setTaskProgress(data.task_progress || null);
      setContextStats(data.context_stats || null);
      if (scrollToBottom) {
        setShouldAutoScroll(true);
        requestAnimationFrame(() => scrollDown("auto"));
      }
      return data.session_id;
    } catch {
      return null;
    }
  }, [scrollDown]);

  const refreshActiveSession = useCallback(async () => {
    if (!activeSessionId) return null;
    return loadSession(activeSessionId, { scrollToBottom: false });
  }, [activeSessionId, loadSession]);

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);

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
    writeKnowledgeCollapsed(knowledgePanelCollapsed);
  }, [knowledgePanelCollapsed]);

  useEffect(() => {
    writeFeishuCollapsed(feishuPanelCollapsed);
  }, [feishuPanelCollapsed]);

  useEffect(() => {
    applyTheme(themePreference);
    return subscribeSystemTheme(() => {
      applyTheme(readThemePreference());
    });
  }, [themePreference]);

  useEffect(() => {
    updateDocumentTitle(Boolean(loading));
  }, [loading]);

  const handleThemeChange = useCallback((preference) => {
    setThemePreference(preference);
    setThemePreferenceState(preference);
  }, []);

  useEffect(() => {
    writeStoredWidth(SESSION_SIDEBAR_WIDTH_KEY, sessionSidebarWidth);
  }, [sessionSidebarWidth]);

  useEffect(() => {
    writeStoredWidth(DEBUG_SIDEBAR_WIDTH_KEY, debugSidebarWidth);
  }, [debugSidebarWidth]);

  useEffect(() => {
    writeStoredWidth(SUBAGENT_SIDEBAR_WIDTH_KEY, subagentSidebarWidth);
  }, [subagentSidebarWidth]);

  useEffect(() => {
    const handleResize = () => {
      const viewport = window.innerWidth || 1200;
      const sideMax = Math.max(SESSION_SIDEBAR_MIN_WIDTH, Math.min(SESSION_SIDEBAR_MAX_WIDTH, Math.floor(viewport * 0.36)));
      const debugMax = Math.max(DEBUG_SIDEBAR_MIN_WIDTH, Math.min(DEBUG_SIDEBAR_MAX_WIDTH, Math.floor(viewport * 0.44)));
      const subagentMax = Math.max(SUBAGENT_SIDEBAR_MIN_WIDTH, Math.min(SUBAGENT_SIDEBAR_MAX_WIDTH, Math.floor(viewport * 0.38)));
      setSessionSidebarWidth((width) => clampNumber(width, SESSION_SIDEBAR_MIN_WIDTH, sideMax));
      setDebugSidebarWidth((width) => clampNumber(width, DEBUG_SIDEBAR_MIN_WIDTH, debugMax));
      setSubagentSidebarWidth((width) => clampNumber(width, SUBAGENT_SIDEBAR_MIN_WIDTH, subagentMax));
    };
    window.addEventListener("resize", handleResize);
    handleResize();
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  useEffect(() => {
    refreshKnowledgeCenter().catch(() => {});
  }, [refreshKnowledgeCenter]);

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
          setActiveSessionId(sessionId);
          activeSessionIdRef.current = sessionId;
          writeLastSessionId(sessionId);
          setMessages([]);
          setSessions((prev) => replaceDraftSession(prev, sessionId));
        }
      } catch {
        const fallbackSessions = await refreshSessions();
        if (cancelled) return;
        if (fallbackSessions?.length) {
          await loadSession(fallbackSessions[0].session_id, { scrollToBottom: true });
          return;
        }
        const sessionId = makeSessionId();
        setActiveSessionId(sessionId);
        activeSessionIdRef.current = sessionId;
        writeLastSessionId(sessionId);
        setMessages([]);
        setSessions((prev) => prev);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadSession, refreshFeishuStatus, refreshSessions]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      refreshFeishuStatus().catch(() => {});
    }, 15000);
    return () => window.clearInterval(timer);
  }, [refreshFeishuStatus]);

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
    activeSessionIdRef.current = sessionId;
    writeLastSessionId(sessionId);
    setMessages([]);
    setSessions((prev) => replaceDraftSession(prev, sessionId));
    setSessionRuns((prev) => replaceRunEvents(replaceRunEvents(replaceRunEvents(prev, sessionId, "debugEvents", []), sessionId, "toolEvents", []), sessionId, "activityItems", []));
    setSubagentTasks([]);
    setSubagentNotifications([]);
    setTaskProgress(null);
    setPendingFiles([]);
    setAttachmentError("");
  }, []);

  const handleRenameSession = useCallback((event, session) => {
    event.preventDefault();
    event.stopPropagation();
    setRenameDialogSession(session);
  }, []);

  const handleRenameSubmit = useCallback(
    async (title) => {
      const session = renameDialogSession;
      if (!session) return;
      const currentTitle = session.title || session.session_id;
      if (!title || title === currentTitle) {
        setRenameDialogSession(null);
        return;
      }
      setSessionDialogBusy(true);
      try {
        let resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(session.session_id)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json; charset=utf-8" },
          body: JSON.stringify({ title }),
        });
        if (resp.status === 404) {
          // Draft session exists only client-side; persist it with the new title.
          resp = await fetch(`${API_BASE}/sessions`, {
            method: "POST",
            headers: { "Content-Type": "application/json; charset=utf-8" },
            body: JSON.stringify({ session_id: session.session_id, title }),
          });
        }
        if (resp.ok) await refreshSessions();
      } finally {
        setSessionDialogBusy(false);
        setRenameDialogSession(null);
      }
    },
    [refreshSessions, renameDialogSession]
  );

  const handleDeleteSession = useCallback((event, session) => {
    event.preventDefault();
    event.stopPropagation();
    setDeleteDialogSession(session);
  }, []);

  const handleDeleteConfirm = useCallback(async () => {
    const session = deleteDialogSession;
    if (!session) return;
    setSessionDialogBusy(true);
    try {
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
          activeSessionIdRef.current = rememberedSessionId;
          writeLastSessionId(rememberedSessionId);
          setMessages([]);
        }
      } else if (readLastSessionId() === session.session_id) {
        if (nextSessions[0]?.session_id) {
          writeLastSessionId(nextSessions[0].session_id);
        }
      }
      await refreshSessions();
    } finally {
      setSessionDialogBusy(false);
      setDeleteDialogSession(null);
    }
  }, [activeSessionId, deleteDialogSession, loadSession, refreshSessions, sessions]);

  const handleStop = useCallback(() => {
    const sessionId = activeSessionIdRef.current;
    if (!sessionId) return;
    const controller = abortControllersRef.current.get(sessionId);
    if (!controller) return;
    controller.abort();
    abortControllersRef.current.delete(sessionId);
    setSessionRuns((prev) => finishSessionRun(prev, sessionId, "stopped", {
      streamingText: "",
      toolEvents: [
        ...(getSessionRun(prev, sessionId).toolEvents || []),
        { type: "progress", message: "请求已被用户停止。", elapsed_seconds: null },
      ],
      debugEvents: [
        ...(getSessionRun(prev, sessionId).debugEvents || []),
        {
          type: "debug",
          stage: "user_abort",
          message: "Request aborted from UI.",
          elapsed_seconds: null,
          details: {},
        },
      ],
    }));
  }, []);

  const handleEditMessage = useCallback((message) => {
    setInput(extractEditableText(message?.content));
    requestAnimationFrame(() => textareaRef.current?.focus());
  }, []);

  const handleFeedback = useCallback(
    (messageIndex, rating, message) => {
      setMessageFeedback((prev) => toggleFeedback(prev, activeSessionId, messageIndex, rating));
      reportFeedback(API_BASE, {
        session_id: activeSessionId,
        message_index: messageIndex,
        rating,
        content_snippet: String(message?.content || "").slice(0, 200),
      }).catch(() => {});
    },
    [activeSessionId]
  );

  const welcomeSuggestions = useMemo(
    () => buildWelcomeSuggestions(welcomeConfig?.welcome_suggestions, installedSkills),
    [welcomeConfig, installedSkills]
  );

  const handleWelcomePick = useCallback((text) => {
    setInput(text);
    requestAnimationFrame(() => textareaRef.current?.focus());
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
      showToast(`技能 ${name} 安装成功`, "success");
    } catch (err) {
      showToast(`技能安装失败: ${err.message}`, "error");
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
      showToast(`技能 ${name} 已卸载`, "success");
    } catch (err) {
      showToast(`技能卸载失败: ${err.message}`, "error");
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
        showToast(`技能执行失败: ${data.error || `HTTP ${resp.status}`}`, "error");
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
      setSkillResult({ type: "error", content: `请求失败: ${err.message}` });
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

  useEffect(() => {
    fetchInstalledSkills();
  }, [fetchInstalledSkills]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await fetch(`${API_BASE}/ui-config`);
        if (!resp.ok) return;
        const data = await resp.json();
        if (!cancelled) setWelcomeConfig(data && typeof data === "object" ? data : null);
      } catch {
        // Optional endpoint; defaults apply when unavailable.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleFeishuInit = useCallback(async () => {
    setFeishuLoading(true);
    try {
      const initPaths = ["/feishu/login/init", "/feishu/init", "/feishu/qr/init"];
      let resp = null;
      let lastPayload = null;
      for (let attempt = 0; attempt < 8; attempt += 1) {
        for (const path of initPaths) {
          resp = await fetch(`${API_BASE}${path}`, { method: "POST" });
          const payload = await readResponsePayload(resp);
          lastPayload = payload;
          if (resp.ok || resp.status !== 404) break;
        }
        if (resp?.ok || resp?.status !== 404) break;
        setFeishuLoginState({ message: "Backend is still warming up Feishu routes. Retrying..." });
        await sleep(750);
      }
      const { ok, data, text } = lastPayload || {};
      if (!resp?.ok) throw new Error((ok && (data?.error || data?.detail)) || text || `HTTP ${resp?.status || "unknown"}`);
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
      const pollPaths = ["/feishu/login/poll", "/feishu/poll", "/feishu/qr/poll"];
      let resp = null;
      let lastPayload = null;
      for (const path of pollPaths) {
        resp = await fetch(`${API_BASE}${path}`, {
          method: "POST",
          headers: { "Content-Type": "application/json; charset=utf-8" },
          body: JSON.stringify({ flow_key: feishuLoginState.flow_key }),
        });
        const payload = await readResponsePayload(resp);
        lastPayload = payload;
        if (resp.ok || resp.status !== 404) break;
      }
      const { ok, data, text } = lastPayload || {};
      if (!resp?.ok) throw new Error((ok && (data?.error || data?.detail)) || text || `HTTP ${resp?.status || "unknown"}`);
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

  const handleFeishuRefreshClick = useCallback(async () => {
    setFeishuLoading(true);
    try {
      const data = await refreshFeishuStatus();
      setFeishuLoginState((prev) => ({
        ...(prev || {}),
        message: data.logged_in ? "Status refreshed: session is active." : "Status refreshed: not logged in.",
      }));
    } catch (err) {
      setFeishuLoginState((prev) => ({
        ...(prev || {}),
        message: `Status refresh failed: ${err.message}`,
      }));
    } finally {
      setFeishuLoading(false);
    }
  }, [refreshFeishuStatus]);

  const importFeishuKnowledge = useCallback(async ({ reference, collection, refresh }) => {
    setKnowledgeLoading(true);
    try {
      const resp = await fetch(`${API_BASE}/knowledge/import/feishu`, {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({ reference, collection, refresh }),
      });
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok || payload.status === "error" || payload.error) {
        const message = remoteKnowledgeErrorMessage(payload, resp.status);
        setKnowledgeStatus({ ...payload, message, counts: { failed: 1 } });
        showToast(message, "error");
        return { ok: false, message, payload };
      }

      setKnowledgeStatus({ ...payload, message: remoteKnowledgeStatusMessage(payload) });
      showToast("飞书文档已加入知识库", "success");
      await refreshKnowledgeCenter();
      return { ok: true, payload };
    } catch (err) {
      const message = err?.message || "知识库导入失败。";
      setKnowledgeStatus({ message, counts: { failed: 1 } });
      return { ok: false, message };
    } finally {
      setKnowledgeLoading(false);
    }
  }, [refreshKnowledgeCenter]);

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

  const handleSend = useCallback(async (overrideText) => {
    const useOverride = typeof overrideText === "string";
    const text = (useOverride ? overrideText : input).trim();
    const filesToUpload = useOverride ? [] : [...pendingFiles];
    const mode = getSubmitMode(sessionRuns, activeSessionId);
    const isAppendMode = mode === "append";
    if (isAppendMode && !text) return;
    if (!isAppendMode && (!text && !filesToUpload.length)) return;
    if (loading && !isAppendMode) return;

    let ensuredSessionId = activeSessionId;
    if (!ensuredSessionId) {
      ensuredSessionId = makeSessionId();
      setActiveSessionId(ensuredSessionId);
      activeSessionIdRef.current = ensuredSessionId;
      writeLastSessionId(ensuredSessionId);
    }

    if (isAppendMode) {
      try {
        const resp = await fetch(getSubmitEndpoint(API_BASE, sessionRuns, ensuredSessionId), {
          method: "POST",
          headers: { "Content-Type": "application/json; charset=utf-8" },
          body: JSON.stringify(getAppendRequestPayload(sessionRuns, ensuredSessionId, text)),
        });
        let payload = {};
        try {
          payload = await resp.json();
        } catch {
          payload = {};
        }
        if (!resp.ok) {
          throw new Error(payload.message || `Append failed: HTTP ${resp.status}`);
        }
        setSessionRuns((prev) => {
          const withAppend = recordAppendCommandEvent(prev, ensuredSessionId, {
            ...payload,
            content: text,
            status: payload.status || "queued",
          });
          return updateSessionRun(withAppend, ensuredSessionId, (current) => ({
            error: "",
            activityItems: mergeActivityItems(current.activityItems || [], {
              state: "working",
              summary: "已收到追加指令，等待注入当前任务。",
              next_step: "下一次模型调用前会带上该追加内容。",
              progress: { current: 2, total: 3, label: "追加指令" },
            }),
            debugEvents: [
              ...(current.debugEvents || []),
              {
                type: "debug",
                stage: "append_command_queued",
                message: "Append command queued for the active run.",
                elapsed_seconds: null,
                details: payload,
              },
            ],
          }));
        });
        if (shouldClearComposerAfterSubmit({ mode, ok: true })) {
          setInput("");
        }
      } catch (err) {
        setSessionRuns((prev) =>
          updateSessionRun(prev, ensuredSessionId, (current) => ({
            error: err.message,
            activityItems: mergeActivityItems(current.activityItems || [], {
              state: "judging",
              summary: "追加指令未发送成功。",
              judgment: err.message,
              next_step: "请确认当前任务仍在运行，或作为新消息发送。",
              progress: { current: 2, total: 3, label: "追加指令" },
            }),
            debugEvents: [
              ...(current.debugEvents || []),
              {
                type: "debug",
                stage: "append_command_rejected",
                message: err.message,
                elapsed_seconds: null,
                details: {},
              },
            ],
          }))
        );
        if (shouldClearComposerAfterSubmit({ mode, ok: false })) {
          setInput("");
        }
      }
      return;
    }

    const controller = new AbortController();
    const runId =
      typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    abortControllersRef.current.set(ensuredSessionId, controller);
    setSessionRuns((prev) => startSessionRun(prev, ensuredSessionId, runId));

    const runState = { assistantContent: "", tools: [], debug: [], activities: [], usage: null, research: null, error: "" };
    const appendVisibleMessage = (message) => {
      if (activeSessionIdRef.current === ensuredSessionId) {
        setMessages((prev) => [...prev, message]);
      }
    };

    function pushActivity(activity) {
      runState.activities = mergeActivityItems(runState.activities, activity);
      setSessionRuns((prev) =>
        updateSessionRun(prev, ensuredSessionId, () => ({
          activityItems: [...runState.activities],
        }))
      );
    }

    function pushToolEvent(event) {
      const last = runState.tools[runState.tools.length - 1];
      if (event?.type === "progress" && last?.type === "progress") {
        runState.tools[runState.tools.length - 1] = event;
      } else {
        runState.tools.push(event);
      }
      setSessionRuns((prev) =>
        updateSessionRun(prev, ensuredSessionId, () => ({
          toolEvents: [...runState.tools],
        }))
      );
    }

    function pushDebugEvent(event) {
      const last = runState.debug[runState.debug.length - 1];
      if (event?.stage === "heartbeat" && last?.stage === "heartbeat") {
        runState.debug[runState.debug.length - 1] = event;
      } else {
        runState.debug.push(event);
      }
      setSessionRuns((prev) =>
        updateSessionRun(prev, ensuredSessionId, () => ({
          debugEvents: [...runState.debug],
        }))
      );
    }

    function dispatchSseEvent(eventType, rawData) {
      if (!eventType || rawData == null || rawData === "") return;
      let parsed;
      try {
        parsed = JSON.parse(rawData);
      } catch {
        parsed = rawData;
      }
      if (parsed && typeof parsed === "object" && parsed.run_id) {
        setSessionRuns((prev) => applyRunAttribution(prev, ensuredSessionId, parsed));
      }

      if (eventType === "text") {
        runState.assistantContent += String(parsed);
        setSessionRuns((prev) => appendRunText(prev, ensuredSessionId, String(parsed)));
      } else if (eventType === "research") {
        runState.research = parsed && typeof parsed === "object" ? parsed : null;
      } else if (eventType === "progress") {
        pushToolEvent({
          type: "progress",
          message: parsed?.message || "仍在工作中...",
          elapsed_seconds: parsed?.elapsed_seconds,
          active_tool: parsed?.active_tool,
        });
      } else if (eventType === "activity") {
        pushActivity(parsed || {});
      } else if (eventType === "debug") {
        const { message, elapsed_seconds, stage, ...details } = parsed || {};
        if (stage === "model_usage" && parsed?.usage && typeof parsed.usage === "object") {
          runState.usage = parsed.usage;
        }
        pushDebugEvent({
          type: "debug",
          message: message || "Debug event",
          elapsed_seconds,
          stage,
          details,
        });
        if (String(stage || "").startsWith("append_command_")) {
          const status =
            stage === "append_command_injected"
              ? "injected"
              : stage === "append_command_not_applied"
                ? "not_applied"
                : stage === "append_command_rejected"
                  ? "rejected"
                  : parsed?.status || "queued";
          setSessionRuns((prev) => {
            const withAppend = parsed?.append_id
              ? recordAppendCommandEvent(prev, ensuredSessionId, { ...parsed, status })
              : prev;
            return updateSessionRun(withAppend, ensuredSessionId, (current) => ({
              activityItems: mergeActivityItems(current.activityItems || [], {
                state: status === "rejected" || status === "not_applied" ? "judging" : "working",
                summary:
                  status === "injected"
                    ? "追加指令已注入当前任务。"
                    : status === "not_applied"
                      ? "追加指令未在任务结束前生效。"
                      : status === "rejected"
                        ? "追加指令被拒绝。"
                        : "追加指令已排队。",
                next_step:
                  status === "injected"
                    ? "继续等待当前任务输出。"
                    : "等待当前任务下一步处理。",
                progress: { current: 2, total: 3, label: "追加指令" },
              }),
            }));
          });
        }
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
        runState.error = parsed?.message || String(parsed || "Request failed");
        pushDebugEvent({
          type: "debug",
          message: parsed?.message || "Unknown backend error",
          elapsed_seconds: parsed?.elapsed_seconds,
          stage: "backend_error",
          details: parsed || {},
        });
      } else if (eventType === "done") {
        runState.assistantContent = String(parsed || runState.assistantContent);
        setSessionRuns((prev) => setRunText(prev, ensuredSessionId, runState.assistantContent));
      }
    }

    try {
      const uploadedFiles = await uploadPendingFiles(
        ensuredSessionId,
        filesToUpload,
        controller.signal
      );
      const requestText = buildAttachmentMessage(text, uploadedFiles);
      const userMsg = { role: "user", content: requestText, tools: [], created_at: new Date().toISOString() };
      if (!useOverride) {
        setInput("");
      }
      setPendingFiles([]);
      setAttachmentError("");
      appendVisibleMessage(userMsg);

      const resp = await fetch(`${API_BASE}/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({
          message: requestText,
          session_id: ensuredSessionId,
          history: currentHistory,
          knowledge_policy: knowledgePolicy,
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

      appendVisibleMessage({
        role: "assistant",
        created_at: new Date().toISOString(),
        content: runState.assistantContent || (runState.error ? `请求失败: ${runState.error}` : "(无文本回复)"),
        tools: runState.tools,
        activities: runState.activities,
        usage: runState.usage || {},
        research: runState.research,
      });
      setSessionRuns((prev) => finishSessionRun(prev, ensuredSessionId, "completed", { streamingText: "" }));
      await refreshSessions();
      if (activeSessionIdRef.current === ensuredSessionId) {
        await loadSession(ensuredSessionId, { scrollToBottom: false });
      }
    } catch (err) {
      if (err.name === "AbortError") {
        appendVisibleMessage({
          role: "assistant",
          content: runState.assistantContent || (runState.error ? `请求失败: ${runState.error}` : "(stopped)"),
          tools: runState.tools,
          activities: runState.activities,
          usage: runState.usage || {},
          research: runState.research,
        });
        setSessionRuns((prev) => finishSessionRun(prev, ensuredSessionId, "stopped", { streamingText: "" }));
        await new Promise((resolve) => window.setTimeout(resolve, 300));
        await refreshSessions();
        if (activeSessionIdRef.current === ensuredSessionId) {
          await loadSession(ensuredSessionId, { scrollToBottom: false });
        }
      } else {
        appendVisibleMessage({
          role: "assistant",
          content: `请求失败: ${err.message}`,
          tools: runState.tools,
          activities: runState.activities,
          usage: runState.usage || {},
          research: runState.research,
        });
        pushDebugEvent({
          type: "debug",
          stage: "frontend_error",
          message: `前端请求失败: ${err.message}`,
          elapsed_seconds: null,
          details: {},
        });
        setSessionRuns((prev) => finishSessionRun(prev, ensuredSessionId, "failed", {
          error: err.message,
          streamingText: "",
        }));
      }
    } finally {
      abortControllersRef.current.delete(ensuredSessionId);
    }
  }, [
    activeSessionId,
    currentHistory,
    input,
    loading,
    pendingFiles,
    knowledgePolicy,
    sessionRuns,
    refreshSessions,
    loadSession,
    uploadPendingFiles,
  ]);

  const handleRegenerate = useCallback(() => {
    if (loading) return;
    const lastUser = [...messages].reverse().find((message) => message.role === "user");
    if (!lastUser) return;
    handleSend(lastUser.content);
  }, [handleSend, loading, messages]);

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

  const lastAssistantIndex = useMemo(
    () => messages.reduce((last, msg, index) => (msg.role === "assistant" ? index : last), -1),
    [messages]
  );

  const visibleSessions = useMemo(
    () =>
      sessions.map((session) => {
        if (!isSessionRunning(sessionRuns, session.session_id)) return session;
        return {
          ...session,
          task_progress: {
            ...(session.task_progress || {}),
            status: "running",
            message: "Agent run started.",
          },
        };
      }),
    [sessions, sessionRuns]
  );

  const startColumnResize = useCallback((column, event) => {
    if (event.button != null && event.button !== 0) return;
    event.preventDefault();
    const startX = event.clientX;
    const config = {
      sessions: {
        initial: sessionSidebarWidth,
        direction: 1,
        min: SESSION_SIDEBAR_MIN_WIDTH,
        max: SESSION_SIDEBAR_MAX_WIDTH,
        setWidth: setSessionSidebarWidth,
      },
      debug: {
        initial: debugSidebarWidth,
        direction: -1,
        min: DEBUG_SIDEBAR_MIN_WIDTH,
        max: DEBUG_SIDEBAR_MAX_WIDTH,
        setWidth: setDebugSidebarWidth,
      },
      subagents: {
        initial: subagentSidebarWidth,
        direction: -1,
        min: SUBAGENT_SIDEBAR_MIN_WIDTH,
        max: SUBAGENT_SIDEBAR_MAX_WIDTH,
        setWidth: setSubagentSidebarWidth,
      },
    }[column];
    if (!config) return;

    const handlePointerMove = (moveEvent) => {
      const delta = moveEvent.clientX - startX;
      config.setWidth(clampNumber(config.initial + delta * config.direction, config.min, config.max));
    };
    const stopResize = () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", stopResize);
      document.body.classList.remove("is-resizing-columns");
    };

    document.body.classList.add("is-resizing-columns");
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", stopResize);
  }, [debugSidebarWidth, sessionSidebarWidth, subagentSidebarWidth]);

  const stepColumnWidth = useCallback((column, delta) => {
    if (column === "sessions") {
      setSessionSidebarWidth((width) => clampNumber(width + delta, SESSION_SIDEBAR_MIN_WIDTH, SESSION_SIDEBAR_MAX_WIDTH));
    } else if (column === "debug") {
      setDebugSidebarWidth((width) => clampNumber(width - delta, DEBUG_SIDEBAR_MIN_WIDTH, DEBUG_SIDEBAR_MAX_WIDTH));
    } else if (column === "subagents") {
      setSubagentSidebarWidth((width) => clampNumber(width - delta, SUBAGENT_SIDEBAR_MIN_WIDTH, SUBAGENT_SIDEBAR_MAX_WIDTH));
    }
  }, []);

  const shellStyle = {
    "--session-sidebar-width": `${sessionSidebarWidth}px`,
    "--debug-sidebar-width": `${debugSidebarWidth}px`,
    "--subagent-sidebar-width": `${subagentSidebarWidth}px`,
  };

  return (
    <div className="app-shell" style={shellStyle}>
      <SessionSidebar
        sessions={visibleSessions}
        activeSessionId={activeSessionId}
        collapsed={sessionSidebarCollapsed}
        onToggleCollapsed={() => setSessionSidebarCollapsed((prev) => !prev)}
        onSelect={loadSession}
        onCreate={handleCreateSession}
        onRename={handleRenameSession}
        onDelete={handleDeleteSession}
      />
      {!sessionSidebarCollapsed ? (
        <ColumnResizer
          label="Resize sessions column"
          onPointerDown={(event) => startColumnResize("sessions", event)}
          onStep={(delta) => stepColumnWidth("sessions", delta)}
        />
      ) : null}

      <div className="app-container" ref={chatAreaRef} onScroll={handleChatScroll}>
        <header className="app-header">
          <div className="header-left">
            <Bot size={22} />
            <h1>Chat Agent</h1>
          </div>
          <div className="header-badges">
            <span className="badge"><Globe size={12} /> 搜索</span>
            <span className="badge"><FileText size={12} /> 文件</span>
            {installedSkills.length ? (
              <span className="badge"><Zap size={12} /> 技能 {installedSkills.length}</span>
            ) : null}
            <span className="badge"><TerminalSquare size={12} /> 调试</span>
            {contextStats ? (
              <span className="badge">
                <Code size={12} />
                ctx {contextStats.history_messages} msg / {contextStats.history_token_estimate} tok
              </span>
            ) : null}
            {contextStats?.usage?.prompt_cache_total_tokens ? (
              <span
                className="cache-ring"
                style={{ "--cache-hit-turn": formatTurn(contextStats.usage.prompt_cache_hit_rate) }}
                title={`缓存命中率: ${formatPercent(contextStats.usage.prompt_cache_hit_rate)} | 命中 ${contextStats.usage.prompt_cache_hit_tokens || 0} / 未命中 ${contextStats.usage.prompt_cache_miss_tokens || 0}`}
                aria-label={`缓存命中率 ${formatPercent(contextStats.usage.prompt_cache_hit_rate)}`}
              >
                {formatPercent(contextStats.usage.prompt_cache_hit_rate)}
              </span>
            ) : null}
          </div>
          <ThemeToggle value={themePreference} onChange={handleThemeChange} />
        </header>

        <div className="top-panels">
          <KnowledgePanel
            documents={knowledgeDocuments}
            knowledgePolicy={knowledgePolicy}
            loading={knowledgeLoading}
            status={knowledgeStatus}
            feishuStatus={feishuStatus}
            collapsed={knowledgePanelCollapsed}
            onKnowledgePolicyChange={setKnowledgePolicy}
            onToggleCollapsed={() => setKnowledgePanelCollapsed((prev) => !prev)}
            onImportFiles={importKnowledgeFiles}
            onImportFeishu={importFeishuKnowledge}
            onOpenFeishuLogin={() => setFeishuPanelCollapsed(false)}
            onSync={syncKnowledge}
            onDeleteDocument={deleteKnowledgeDocument}
            onOpenCenter={() => {
              setKnowledgeCenterOpen(true);
              refreshKnowledgeCenter().catch(() => {});
            }}
          />
          <FeishuLoginPanel
            status={feishuStatus}
            loginState={feishuLoginState}
            loading={feishuLoading}
            polling={feishuPolling}
            collapsed={feishuPanelCollapsed}
            onToggleCollapsed={() => setFeishuPanelCollapsed((prev) => !prev)}
            onInit={handleFeishuInit}
            onRefresh={handleFeishuRefreshClick}
            onLogout={handleFeishuLogout}
          />
        </div>

        <main className="chat-area">
          <div className="messages-container">
            {!loading && !messages.length ? (
              <Welcome suggestions={welcomeSuggestions} onPick={handleWelcomePick} />
            ) : null}
            {messages.map((msg, i) => {
              const feedbackRating = (messageFeedback[activeSessionId] || {})[i] || "";
              return (
                <div key={i} className="message-group">
                  <div className={`message ${msg.role === "user" ? "user-message" : "assistant-message"}`}>
                    <div className="message-avatar">
                      {msg.role === "user" ? <User size={16} /> : <Bot size={16} />}
                    </div>
                    <div className="message-body">
                      <ChatMessageContent content={msg.content} />
                      <MessageTokenUsage role={msg.role} usage={msg.usage} />
                      {msg.role === "assistant" ? <KnowledgeCitations tools={msg.tools} /> : null}
                      {msg.role === "assistant" ? <ResearchProvenance research={msg.research} /> : null}
                      {msg.created_at ? (
                        <span className="message-time" title={formatFullTime(msg.created_at)}>
                          {formatRelativeTime(msg.created_at)}
                        </span>
                      ) : null}
                    </div>
                  </div>
                  {msg.role === "assistant" ? (
                    <AssistantMessageActions
                      content={msg.content}
                      canRegenerate={i === lastAssistantIndex && !loading}
                      onRegenerate={handleRegenerate}
                      feedback={feedbackRating}
                      onFeedback={(rating) => handleFeedback(i, rating, msg)}
                    />
                  ) : (
                    <UserMessageActions onEdit={() => handleEditMessage(msg)} />
                  )}
                  {msg.role === "assistant" && isErrorMessage(msg) && !loading ? (
                    <button type="button" className="retry-btn" onClick={handleRegenerate}>
                      <RefreshCw size={13} />
                      重试
                    </button>
                  ) : null}
                  {msg.role === "assistant" && <ToolEvents events={msg.tools} />}
                  {msg.role === "assistant" && <AgentActivityTimeline items={msg.activities} />}
                </div>
              );
            })}

            <ToolEvents events={toolEvents} />
            <AgentActivityTimeline items={activityItems} />

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
          {!shouldAutoScroll ? (
            <button
              type="button"
              className="jump-bottom-btn"
              aria-label="回到底部"
              title="回到底部"
              onClick={() => {
                setShouldAutoScroll(true);
                scrollDown("smooth");
              }}
            >
              <ChevronDown size={16} />
            </button>
          ) : null}
        </main>

        <footer
          className={`input-area ${isDraggingFiles ? "is-dragging" : ""}`}
          onDragEnter={handleDragEnter}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <div className="composer-shell">
            {isDraggingFiles ? (
              <div className="composer-drop-prompt">
                {isDraggingImages ? <FileImage size={18} /> : <Paperclip size={18} />}
                {isDraggingImages ? "松开以粘贴图片" : "松开以添加附件"}
              </div>
            ) : null}
            {pendingFiles.length ? (
              <div className="attachment-list">
                {pendingFiles.map((file, index) => (
                  <div className="attachment-chip" key={`${file.name}-${file.size}-${file.lastModified}`}>
                    {isImageFile(file) ? <FileImage size={14} /> : <FileText size={14} />}
                    <span className="attachment-name" title={file.name}>{file.name}</span>
                    <span className="attachment-size">{formatFileSize(file.size)}</span>
                    <button
                      type="button"
                      className="attachment-remove"
                      onClick={() => removePendingFile(index)}
                      aria-label={`移除 ${file.name}`}
                      disabled={loading}
                    >
                      <X size={13} />
                    </button>
                  </div>
                ))}
              </div>
            ) : null}
            <div className="input-wrapper">
              <button
                className="skill-slash-btn"
                onClick={handleOpenSkillPopup}
                title="打开技能 (/skill)"
                disabled={loading}
              >
                <Zap size={16} />
              </button>
              <button
                type="button"
                className="attach-btn"
                onClick={() => fileInputRef.current?.click()}
                title="附加文件"
                disabled={loading || pendingFiles.length >= MAX_ATTACHMENTS}
              >
                <Paperclip size={17} />
              </button>
              <input
                ref={fileInputRef}
                className="file-input"
                type="file"
                multiple
                onChange={(event) => {
                  addPendingFiles(event.target.files);
                  event.target.value = "";
                }}
                disabled={loading}
              />
              <textarea
                ref={textareaRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                onPaste={handlePaste}
                placeholder={appendMode ? "追加指令到当前运行中的任务" : "发送消息,或拖入文件"}
                rows={1}
                disabled={false}
              />
              {loading ? (
                <button className="stop-btn" onClick={handleStop} title="停止当前请求">
                  <Square size={16} />
                </button>
              ) : null}
              <button
                className="send-btn"
                title={appendMode ? "追加指令" : "发送消息"}
                aria-label={appendMode ? "追加指令" : "发送消息"}
                onClick={() => handleSend()}
                disabled={appendMode ? !input.trim() : (!input.trim() && !pendingFiles.length)}
              >
                {appendMode ? <Send size={18} /> : loading ? <Loader2 className="spin" size={18} /> : <Send size={18} />}
              </button>
            </div>
            <div className={`composer-meta ${attachmentError ? "has-error" : ""}`}>
              <span>{attachmentError || (appendMode ? "当前任务运行中：Enter 会追加到当前任务 · Shift+Enter 换行" : "Enter 发送 · Shift+Enter 换行 · 可拖入或粘贴图片 · 最多 10 个文件,每个 25 MB")}</span>
              <span>{input.length.toLocaleString()} 字符</span>
            </div>
          </div>
        </footer>
      </div>

      {debugOpen ? (
        <ColumnResizer
          label="Resize debug column"
          onPointerDown={(event) => startColumnResize("debug", event)}
          onStep={(delta) => stepColumnWidth("debug", delta)}
        />
      ) : null}
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
        onClear={() => setSessionRuns((prev) => replaceRunEvents(prev, activeSessionId, "debugEvents", []))}
      />
      {debugOpen ? (
        <ColumnResizer
          label="Resize subagents column"
          onPointerDown={(event) => startColumnResize("subagents", event)}
          onStep={(delta) => stepColumnWidth("subagents", delta)}
        />
      ) : null}
      {debugOpen ? (
        <aside className="subagent-sidebar">
          <TaskProgressPanel progress={taskProgress} onRefresh={refreshActiveSession} />
          <div className="subagent-sidebar-header">
            <div>
              <div className="debug-sidebar-title">子代理</div>
              <div className="debug-sidebar-subtitle">{subagentTasks.length} 个任务</div>
            </div>
            <button className="session-toolbar-btn" onClick={refreshActiveSession} title="刷新子代理">
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
              : <div className="debug-empty">暂无子代理任务。</div>}
          </div>
        </aside>
      ) : null}

      <KnowledgeCenter
        open={knowledgeCenterOpen}
        documents={knowledgeDocuments}
        sources={knowledgeSources}
        detail={knowledgeDetail}
        detailLoading={knowledgeDetailLoading}
        loading={knowledgeLoading}
        onClose={() => setKnowledgeCenterOpen(false)}
        onInspect={inspectKnowledgeDocument}
        onRefresh={refreshKnowledgeCenter}
        onDeleteDocument={deleteKnowledgeDocument}
      />

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

      <PromptDialog
        open={Boolean(renameDialogSession)}
        title="重命名会话"
        label="会话标题"
        initialValue={renameDialogSession?.title || renameDialogSession?.session_id || ""}
        submitLabel="保存"
        loading={sessionDialogBusy}
        onSubmit={handleRenameSubmit}
        onClose={() => setRenameDialogSession(null)}
      />
      <ConfirmDialog
        open={Boolean(deleteDialogSession)}
        title="删除会话"
        message={`确认删除会话"${deleteDialogSession?.title || deleteDialogSession?.session_id}"?该操作不可撤销。`}
        confirmLabel="删除"
        danger
        loading={sessionDialogBusy}
        onConfirm={handleDeleteConfirm}
        onCancel={() => setDeleteDialogSession(null)}
      />

      <ToastHost />
    </div>
  );
}
