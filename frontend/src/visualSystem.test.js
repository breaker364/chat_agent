// @vitest-environment jsdom

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const cssPath = path.join(path.dirname(fileURLToPath(import.meta.url)), "App.css");
const css = readFileSync(cssPath, "utf-8");

describe("visual system tokens and surfaces", () => {
  it("uses an enlarged, layered radius scale", () => {
    expect(css).toMatch(/--radius-sm: 8px;/);
    expect(css).toMatch(/--radius: 12px;/);
    expect(css).toMatch(/--radius-lg: 16px;/);
    expect(css).toMatch(/--radius-xl: 22px;/);
  });

  it("renders the composer as a pill shape", () => {
    const block = css.match(/\.input-wrapper \{[\s\S]*?\}/);
    expect(block).toBeTruthy();
    expect(block[0]).toMatch(/border-radius:\s*var\(--radius-full\)/);
  });

  it("defines the standard motion set", () => {
    for (const keyframe of ["message-in", "dialog-in", "overlay-in", "toast-in", "working-pulse"]) {
      expect(css).toMatch(new RegExp(`@keyframes ${keyframe}\\b`));
    }
    expect(css).toMatch(/\.messages-container > \.message-group \{[^}]*animation:\s*message-in/);
  });

  it("disables decorative motion under prefers-reduced-motion", () => {
    const block = css.match(/@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?\n\}/);
    expect(block).toBeTruthy();
    expect(block[0]).toMatch(/animation:\s*none/);
    expect(block[0]).toMatch(/transition-duration/);
  });

  it("applies glass material to the sticky header and composer", () => {
    const header = css.match(/\.app-header \{[\s\S]*?\}/);
    expect(header[0]).toMatch(/position:\s*sticky/);
    expect(header[0]).toMatch(/backdrop-filter:\s*blur/);
    expect(header[0]).toMatch(/var\(--glass-bg\)/);

    const composer = css.match(/\.input-area \{[\s\S]*?\}/);
    expect(composer[0]).toMatch(/position:\s*sticky/);
    expect(composer[0]).toMatch(/backdrop-filter:\s*blur/);
    expect(composer[0]).toMatch(/var\(--glass-bg\)/);
  });

  it("provides a solid fallback for browsers without backdrop-filter", () => {
    expect(css).toMatch(/@supports not \(\s*backdrop-filter:\s*blur\(1px\)\s*\)/);
    const block = css.match(/@supports not \(\s*backdrop-filter:\s*blur\(1px\)\s*\) \{[\s\S]*?\n\}/);
    expect(block[0]).toMatch(/\.app-header/);
    expect(block[0]).toMatch(/\.input-area/);
  });

  it("keeps persistent blurred surfaces within the agreed budget", () => {
    const blocks = css.match(/^[^@{}]*\{[^}]*backdrop-filter:\s*blur[^}]*\}/gm) || [];
    const persistent = blocks.filter((b) => /\.app-header|\.input-area/.test(b));
    expect(persistent.length).toBe(2);
    const transient = blocks.filter((b) => /overlay|\.skill-popup/.test(b));
    expect(transient.length).toBeLessThanOrEqual(4);
  });

  it("animates overlays and dialogs on open", () => {
    expect(css).toMatch(/\.feishu-import-dialog[^{]*\{[^}]*animation:\s*dialog-in/);
    expect(css).toMatch(/\.skill-popup[^{]*\{[^}]*animation:\s*dialog-in/);
  });
});
