// The API and persisted configuration remain integer seconds. Only the UI uses hours.
export function hoursToSeconds(value: number|string): number {
  const hours = typeof value === 'string' && value.trim() === '' ? NaN : Number(value)
  const seconds = hours * 3600
  const integer = Math.round(seconds)
  // Permit only floating-point representation error, not fractional-second rounding.
  // This also preserves every existing integer-second setting on load/save.
  const tolerance = Number.EPSILON * Math.max(1, Math.abs(seconds)) * 4
  if (!Number.isFinite(seconds) || integer < 30 || integer > 604800 || Math.abs(seconds-integer) > tolerance) {
    throw new Error('运行间隔须对应整数秒，范围为 30 秒至 168 h。')
  }
  return integer
}
