import { useState } from "react";
import { Check, Copy, Pencil, RefreshCw, ThumbsDown, ThumbsUp } from "lucide-react";

import { showToast } from "./toast";

async function copyMessageText(text) {
  if (!navigator.clipboard?.writeText) {
    showToast("复制失败", "error");
    return false;
  }
  try {
    await navigator.clipboard.writeText(text);
    showToast("已复制到剪贴板", "success");
    return true;
  } catch {
    showToast("复制失败", "error");
    return false;
  }
}

export function AssistantMessageActions({ content, canRegenerate, onRegenerate, feedback, onFeedback }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (await copyMessageText(content)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    }
  };

  return (
    <div className="message-actions" role="toolbar" aria-label="消息操作">
      <button
        type="button"
        className="message-action-btn"
        onClick={handleCopy}
        aria-label="复制回复"
        title="复制回复"
      >
        {copied ? <Check size={13} /> : <Copy size={13} />}
      </button>
      {canRegenerate ? (
        <button
          type="button"
          className="message-action-btn"
          onClick={onRegenerate}
          aria-label="重新生成"
          title="重新生成"
        >
          <RefreshCw size={13} />
        </button>
      ) : null}
      <button
        type="button"
        className={`message-action-btn ${feedback === "up" ? "active" : ""}`}
        onClick={() => onFeedback("up")}
        aria-label="有帮助"
        aria-pressed={feedback === "up"}
        title="有帮助"
      >
        <ThumbsUp size={13} />
      </button>
      <button
        type="button"
        className={`message-action-btn ${feedback === "down" ? "active" : ""}`}
        onClick={() => onFeedback("down")}
        aria-label="没帮助"
        aria-pressed={feedback === "down"}
        title="没帮助"
      >
        <ThumbsDown size={13} />
      </button>
    </div>
  );
}

export function UserMessageActions({ onEdit }) {
  return (
    <div className="message-actions message-actions-user" role="toolbar" aria-label="消息操作">
      <button
        type="button"
        className="message-action-btn"
        onClick={onEdit}
        aria-label="编辑消息"
        title="编辑消息"
      >
        <Pencil size={13} />
      </button>
    </div>
  );
}
