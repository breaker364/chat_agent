export const THEME_STORAGE_KEY = "chat-agent:theme";
export const THEME_OPTIONS = ["light", "dark", "system"];
export const APP_TITLE = "Chat Agent";

function normalizePreference(preference) {
  return THEME_OPTIONS.includes(preference) ? preference : "system";
}

export function readThemePreference() {
  try {
    return normalizePreference(window.localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    return "system";
  }
}

export function writeThemePreference(preference) {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, normalizePreference(preference));
  } catch {
    // Ignore localStorage failures.
  }
}

export function systemPrefersDark() {
  if (typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function resolveTheme(preference) {
  const normalized = normalizePreference(preference);
  if (normalized === "system") return systemPrefersDark() ? "dark" : "light";
  return normalized;
}

export function applyTheme(preference) {
  const resolved = resolveTheme(preference);
  document.documentElement.dataset.theme = resolved;
  return resolved;
}

export function setThemePreference(preference) {
  const normalized = normalizePreference(preference);
  writeThemePreference(normalized);
  return applyTheme(normalized);
}

export function subscribeSystemTheme(callback) {
  if (typeof window.matchMedia !== "function") return () => {};
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  if (typeof media.addEventListener !== "function") return () => {};
  const handler = () => callback();
  media.addEventListener("change", handler);
  return () => media.removeEventListener("change", handler);
}

export function updateDocumentTitle(running) {
  document.title = running ? `运行中 · ${APP_TITLE}` : APP_TITLE;
}
