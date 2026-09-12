import { useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";

import { dismissToast, subscribeToasts } from "./toast";

const ICON_BY_TYPE = { success: CheckCircle2, error: AlertCircle, info: Info };

export default function ToastHost() {
  const [toasts, setToasts] = useState([]);

  useEffect(
    () =>
      subscribeToasts((event) => {
        if (event.kind === "push") {
          setToasts((prev) => [...prev, event.toast]);
        } else if (event.kind === "dismiss") {
          setToasts((prev) => prev.filter((toast) => toast.id !== event.id));
        }
      }),
    []
  );

  return (
    <div className="toast-host" aria-live="polite">
      {toasts.map((toast) => {
        const Icon = ICON_BY_TYPE[toast.type] || Info;
        return (
          <div className={`toast-item toast-${toast.type}`} key={toast.id} role="status">
            <Icon size={15} />
            <span className="toast-message">{toast.message}</span>
            <button
              type="button"
              className="toast-close"
              onClick={() => dismissToast(toast.id)}
              aria-label="关闭提示"
            >
              <X size={13} />
            </button>
          </div>
        );
      })}
    </div>
  );
}
