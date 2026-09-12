import { describe, expect, it } from "vitest";

import { DEBUG_AUTO_COLLAPSE_MAX_WIDTH, initialDebugSidebarOpen } from "./layoutPrefs";

describe("initialDebugSidebarOpen", () => {
  it("keeps the debug sidebar open on wide viewports when not manually collapsed", () => {
    expect(initialDebugSidebarOpen(1500, false)).toBe(true);
    expect(initialDebugSidebarOpen(DEBUG_AUTO_COLLAPSE_MAX_WIDTH, false)).toBe(true);
  });

  it("starts closed on narrow viewports regardless of the stored preference", () => {
    expect(initialDebugSidebarOpen(1100, false)).toBe(false);
    expect(initialDebugSidebarOpen(1100, true)).toBe(false);
    expect(initialDebugSidebarOpen(1280, true)).toBe(false);
    expect(initialDebugSidebarOpen(1359, false)).toBe(false);
    expect(initialDebugSidebarOpen(960, false)).toBe(false);
  });

  it("honours a manual collapse on wide viewports", () => {
    expect(initialDebugSidebarOpen(1500, true)).toBe(false);
  });

  it("falls back to the current viewport when width is not a number", () => {
    expect(typeof initialDebugSidebarOpen(undefined, false)).toBe("boolean");
  });
});
