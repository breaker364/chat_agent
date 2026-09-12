const listeners = new Set();
let nextId = 1;

export function subscribeToasts(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function dismissToast(id) {
  listeners.forEach((listener) => listener({ kind: "dismiss", id }));
}

export function showToast(message, type = "info", duration = 3200) {
  const toast = { id: nextId++, message: String(message || ""), type };
  listeners.forEach((listener) => listener({ kind: "push", toast }));
  if (duration > 0) {
    window.setTimeout(() => dismissToast(toast.id), duration);
  }
  return toast.id;
}
