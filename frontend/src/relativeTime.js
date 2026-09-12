const MINUTE_MS = 60 * 1000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

function toDate(value) {
  if (value instanceof Date) return value;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatRelativeTime(value, now = new Date()) {
  const date = toDate(value);
  if (!date) return "";
  const diffMs = now.getTime() - date.getTime();
  if (diffMs < 0) return "刚刚";
  if (diffMs < MINUTE_MS) return "刚刚";
  if (diffMs < HOUR_MS) return `${Math.floor(diffMs / MINUTE_MS)} 分钟前`;
  if (diffMs < DAY_MS) return `${Math.floor(diffMs / HOUR_MS)} 小时前`;
  const days = Math.floor(diffMs / DAY_MS);
  if (days === 1) return "昨天";
  if (days < 7) return `${days} 天前`;
  return date.toLocaleDateString();
}

export function formatFullTime(value) {
  const date = toDate(value);
  if (!date) return "";
  return date.toLocaleString();
}
