// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  THEME_OPTIONS,
  THEME_STORAGE_KEY,
  applyTheme,
  readThemePreference,
  resolveTheme,
  setThemePreference,
  subscribeSystemTheme,
  updateDocumentTitle,
} from "./theme";

function mockMatchMedia(dark) {
  return vi.fn().mockImplementation((query) => ({
    matches: query === "(prefers-color-scheme: dark)" ? dark : false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
}

describe("theme helpers", () => {
  afterEach(() => {
    window.localStorage.clear();
    vi.restoreAllMocks();
    delete document.documentElement.dataset.theme;
  });

  it("defaults to system preference when nothing is stored", () => {
    expect(readThemePreference()).toBe("system");
  });

  it("rejects unknown stored preferences", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "sepia");
    expect(readThemePreference()).toBe("system");
  });

  it("resolves system preference by media query", () => {
    window.matchMedia = mockMatchMedia(true);
    expect(resolveTheme("system")).toBe("dark");
    window.matchMedia = mockMatchMedia(false);
    expect(resolveTheme("system")).toBe("light");
  });

  it("resolves explicit light and dark preferences directly", () => {
    expect(resolveTheme("dark")).toBe("dark");
    expect(resolveTheme("light")).toBe("light");
  });

  it("applies the resolved theme to the document element", () => {
    window.matchMedia = mockMatchMedia(true);
    expect(applyTheme("system")).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("persists a preference and applies it", () => {
    expect(setThemePreference("dark")).toBe("dark");
    expect(readThemePreference()).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("exposes the supported option set used by the toggle", () => {
    expect(THEME_OPTIONS).toEqual(["light", "dark", "system"]);
  });

  it("notifies subscribers when the system theme changes", () => {
    const changeHandlers = [];
    window.matchMedia = vi.fn().mockImplementation(() => ({
      matches: false,
      addEventListener: (event, handler) => {
        if (event === "change") changeHandlers.push(handler);
      },
      removeEventListener: vi.fn(),
    }));
    const listener = vi.fn();
    const unsubscribe = subscribeSystemTheme(listener);
    changeHandlers.forEach((handler) => handler());
    expect(listener).toHaveBeenCalled();
    unsubscribe();
  });

  it("updates the document title for running and idle states", () => {
    updateDocumentTitle(true);
    expect(document.title).toContain("Chat Agent");
    expect(document.title).not.toBe("Chat Agent");
    updateDocumentTitle(false);
    expect(document.title).toBe("Chat Agent");
  });
});
