const DAY_MS = 24 * 60 * 60 * 1000;

export function groupSessionsByDate(sessions, now = new Date()) {
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const buckets = [
    { label: "今天", items: [] },
    { label: "昨天", items: [] },
    { label: "本周", items: [] },
    { label: "更早", items: [] },
  ];

  (sessions || []).forEach((session) => {
    const raw = session?.updated_at || session?.created_at;
    const date = raw ? new Date(raw) : null;
    if (!date || Number.isNaN(date.getTime())) {
      buckets[3].items.push(session);
      return;
    }
    const time = date.getTime();
    if (time >= startOfToday) buckets[0].items.push(session);
    else if (time >= startOfToday - DAY_MS) buckets[1].items.push(session);
    else if (time >= startOfToday - 6 * DAY_MS) buckets[2].items.push(session);
    else buckets[3].items.push(session);
  });

  return buckets.filter((bucket) => bucket.items.length);
}
