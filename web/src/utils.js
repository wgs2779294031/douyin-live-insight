export function fmtN(n) {
  if (n == null) return '—'
  if (n >= 1e8) return (n / 1e8).toFixed(1) + '亿'
  if (n >= 1e4) return (n / 1e4).toFixed(1) + 'w'
  return String(n)
}

export function fmtTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toTimeString().slice(0, 8)
}

export function fmtMin(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0')
}

export function fmtDate(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
    String(d.getDate()).padStart(2, '0')
}

// 秒 → 中文时长（冷场累计用）。0 显示 0 秒，别显示「—」，那会被读成没数据。
export function fmtDur(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0))
  if (s < 60) return s + ' 秒'
  const m = Math.floor(s / 60)
  if (m < 60) return m + ' 分' + (s % 60 ? (s % 60) + ' 秒' : '')
  return Math.floor(m / 60) + ' 小时' + (m % 60 ? (m % 60) + ' 分' : '')
}

export function pct(v) {
  if (v == null) return '—'
  return Math.round(v * 100) + '%'
}

export function levelText(level) {
  return ({ high: '高', mid: '中', low: '低' }[level] || level || '—')
}
