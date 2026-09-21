// The API and persisted configuration remain integer seconds.
export function hoursToSeconds(value: number|string): number {
  return intervalToSeconds(value, 3600, '运行间隔须对应整数秒，范围为 30 秒至 168 h。')
}

export function minutesToSeconds(value: number|string): number {
  return intervalToSeconds(value, 60, 'IP 获取间隔须对应整数秒，范围为 0.5 至 10080 min。')
}

function intervalToSeconds(value: number|string, factor: number, message: string): number {
  const units = typeof value === 'string' && value.trim() === '' ? NaN : Number(value)
  const seconds = units * factor
  const integer = Math.round(seconds)
  // Permit only floating-point representation error, not fractional-second rounding.
  // This also preserves every existing integer-second setting on load/save.
  const tolerance = Number.EPSILON * Math.max(1, Math.abs(seconds)) * 4
  if (!Number.isFinite(seconds) || integer < 30 || integer > 604800 || Math.abs(seconds-integer) > tolerance) {
    throw new Error(message)
  }
  return integer
}
