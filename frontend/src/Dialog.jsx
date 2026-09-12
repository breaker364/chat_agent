import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";

const FOCUSABLE_SELECTOR = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

export function Dialog({ open, title, onClose, children, footer, loading = false }) {
  const dialogRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const handleKeyDown = (event) => {
      if (event.key === "Escape" && !loading) {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const node = dialogRef.current;
      if (!node) return;
      const focusable = Array.from(node.querySelectorAll(FOCUSABLE_SELECTOR)).filter(
        (element) => !element.disabled
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    const initial = dialogRef.current?.querySelector("[data-autofocus]") ||
      dialogRef.current?.querySelector(FOCUSABLE_SELECTOR);
    initial?.focus();
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, loading, onClose]);

  if (!open) return null;

  return (
    <div
      className="dialog-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !loading) onClose();
      }}
    >
      <section className="dialog-panel" ref={dialogRef}>
        <header className="dialog-header">
          <span className="dialog-title">{title}</span>
          <button
            type="button"
            className="dialog-close"
            onClick={onClose}
            disabled={loading}
            aria-label="关闭"
          >
            <X size={16} />
          </button>
        </header>
        {children}
        {footer ? <footer className="dialog-footer">{footer}</footer> : null}
      </section>
    </div>
  );
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "确认",
  danger = false,
  loading = false,
  onConfirm,
  onCancel,
}) {
  return (
    <Dialog
      open={open}
      title={title}
      onClose={onCancel}
      loading={loading}
      footer={
        <>
          <button type="button" className="dialog-btn" onClick={onCancel} disabled={loading}>
            取消
          </button>
          <button
            type="button"
            className={`dialog-btn ${danger ? "dialog-btn-danger" : "dialog-btn-primary"}`}
            onClick={onConfirm}
            disabled={loading}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      <p className="dialog-message">{message}</p>
    </Dialog>
  );
}

export function PromptDialog({
  open,
  title,
  label,
  initialValue = "",
  submitLabel = "确认",
  loading = false,
  onSubmit,
  onClose,
}) {
  const [value, setValue] = useState(initialValue);

  useEffect(() => {
    if (open) setValue(initialValue);
  }, [open, initialValue]);

  const handleSubmit = (event) => {
    event.preventDefault();
    onSubmit(value.trim());
  };

  return (
    <Dialog
      open={open}
      title={title}
      onClose={onClose}
      loading={loading}
      footer={
        <>
          <button type="button" className="dialog-btn" onClick={onClose} disabled={loading}>
            取消
          </button>
          <button
            type="submit"
            form="dialog-prompt-form"
            className="dialog-btn dialog-btn-primary"
            disabled={loading}
          >
            {submitLabel}
          </button>
        </>
      }
    >
      <form id="dialog-prompt-form" className="dialog-form" onSubmit={handleSubmit}>
        <label className="dialog-field">
          <span>{label}</span>
          <input
            value={value}
            onChange={(event) => setValue(event.target.value)}
            disabled={loading}
            data-autofocus
          />
        </label>
      </form>
    </Dialog>
  );
}
