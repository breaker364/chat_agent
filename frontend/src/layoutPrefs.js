export const DEBUG_AUTO_COLLAPSE_MAX_WIDTH = 1360;

export function initialDebugSidebarOpen(width, storedCollapsed) {
  const viewport =
    typeof width === "number" && Number.isFinite(width)
      ? width
      : typeof window !== "undefined"
        ? window.innerWidth
        : DEBUG_AUTO_COLLAPSE_MAX_WIDTH;
  if (viewport < DEBUG_AUTO_COLLAPSE_MAX_WIDTH) return false;
  return !storedCollapsed;
}
