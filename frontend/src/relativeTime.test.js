import { describe, expect, it } from "vitest";

import { formatFullTime, formatRelativeTime } from "./relativeTime";

const NOW = new Date("2026-09-12T12:00:00Z");

describe("formatRelativeTime", () => {
  it("returns an empty string for missing or invalid values", () => {
    expect(formatRelativeTime("", NOW)).toBe("");
    expect(formatRelativeTime("not-a-date", NOW)).toBe("");
  });

  it("labels recent timestamps as just now", () => {
    expect(formatRelativeTime("2026-09-12T11:59:30Z", NOW)).toBe("刚刚");
  });

  it("labels minutes and hours", () => {
    expect(formatRelativeTime("2026-09-12T11:58:00Z", NOW)).toBe("2 分钟前");
    expect(formatRelativeTime("2026-09-12T09:00:00Z", NOW)).toBe("3 小时前");
  });

  it("labels yesterday and days within a week", () => {
    expect(formatRelativeTime("2026-09-11T12:00:00Z", NOW)).toBe("昨天");
    expect(formatRelativeTime("2026-09-09T12:00:00Z", NOW)).toBe("3 天前");
  });

  it("falls back to the locale date beyond a week", () => {
    const value = "2026-08-20T12:00:00Z";
    expect(formatRelativeTime(value, NOW)).toBe(new Date(value).toLocaleDateString());
  });
});

describe("formatFullTime", () => {
  it("renders a full timestamp", () => {
    expect(formatFullTime("2026-09-12T09:00:00Z")).toBe(new Date("2026-09-12T09:00:00Z").toLocaleString());
    expect(formatFullTime("")).toBe("");
  });
});
