import { describe, expect, it } from "vitest";

import { groupSessionsByDate } from "./sessionGroups";

const NOW = new Date("2026-09-12T12:00:00Z");

function session(id, updatedAt) {
  return { session_id: id, title: id, updated_at: updatedAt };
}

describe("groupSessionsByDate", () => {
  it("buckets sessions into today, yesterday, this week and earlier", () => {
    const groups = groupSessionsByDate(
      [
        session("a", "2026-09-12T08:00:00Z"),
        session("b", "2026-09-11T09:00:00Z"),
        session("c", "2026-09-08T09:00:00Z"),
        session("d", "2026-08-01T09:00:00Z"),
      ],
      NOW
    );
    expect(groups.map((group) => group.label)).toEqual(["今天", "昨天", "本周", "更早"]);
    expect(groups[0].items[0].session_id).toBe("a");
    expect(groups[3].items[0].session_id).toBe("d");
  });

  it("drops empty buckets", () => {
    const groups = groupSessionsByDate([session("a", "2026-09-12T08:00:00Z")], NOW);
    expect(groups).toHaveLength(1);
    expect(groups[0].label).toBe("今天");
  });

  it("puts sessions without a usable timestamp under 更早", () => {
    const groups = groupSessionsByDate([{ session_id: "x", title: "x" }], NOW);
    expect(groups).toHaveLength(1);
    expect(groups[0].label).toBe("更早");
  });

  it("returns an empty list for no sessions", () => {
    expect(groupSessionsByDate([], NOW)).toEqual([]);
  });
});
