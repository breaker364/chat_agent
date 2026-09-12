import { describe, expect, it } from "vitest";

import { DEFAULT_WELCOME_SUGGESTIONS, buildWelcomeSuggestions } from "./welcomeSuggestions";

describe("buildWelcomeSuggestions", () => {
  it("falls back to generic defaults", () => {
    const suggestions = buildWelcomeSuggestions(null, []);
    expect(suggestions).toEqual(DEFAULT_WELCOME_SUGGESTIONS);
  });

  it("prioritizes a configured list over defaults", () => {
    const suggestions = buildWelcomeSuggestions(["自定义建议一", "自定义建议二"], []);
    expect(suggestions[0]).toBe("自定义建议一");
    expect(suggestions[1]).toBe("自定义建议二");
    expect(suggestions).not.toContain(DEFAULT_WELCOME_SUGGESTIONS[0]);
  });

  it("appends suggestions derived from installed skills", () => {
    const suggestions = buildWelcomeSuggestions(null, [
      { name: "report-lint", description: "检查报告格式" },
      { name: "doc-export", description: "" },
    ]);
    expect(suggestions.some((item) => item.includes("report-lint"))).toBe(true);
    expect(suggestions.some((item) => item.includes("检查报告格式"))).toBe(true);
    expect(suggestions.some((item) => item.includes("doc-export"))).toBe(true);
  });

  it("ignores malformed config and skill entries", () => {
    const suggestions = buildWelcomeSuggestions("not-an-array", [{ description: "no name" }, null]);
    expect(suggestions).toEqual(DEFAULT_WELCOME_SUGGESTIONS);
  });
});
