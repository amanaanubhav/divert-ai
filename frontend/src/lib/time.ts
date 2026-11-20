/** Offset (minutes east of UTC) of `timeZone` at the instant `date`. */
export function tzOffsetMinutes(date: Date, timeZone: string): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(date)
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? '0')
  const asUtc = Date.UTC(
    get('year'),
    get('month') - 1,
    get('day'),
    get('hour'),
    get('minute'),
    get('second'),
  )
  return Math.round((asUtc - Math.floor(date.getTime() / 1000) * 1000) / 60_000)
}

/**
 * ISO instant of local midnight of `yyyy-mm-dd` in `timeZone`
 * (e.g. `2026-11-17` in Asia/Kolkata -> `2026-11-16T18:30:00.000Z`).
 */
export function zonedDayStartIso(day: string, timeZone: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day)
  if (!m) throw new Error(`bad date ${day}`)
  const utcMidnight = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
  // Two passes handle a DST change between the guess and the answer.
  let t = utcMidnight - tzOffsetMinutes(new Date(utcMidnight), timeZone) * 60_000
  t = utcMidnight - tzOffsetMinutes(new Date(t), timeZone) * 60_000
  return new Date(t).toISOString()
}

/** `yyyy-mm-dd` of the instant in `timeZone`. */
export function zonedDay(date: Date, timeZone: string): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(date)
}

/** Add whole days to a `yyyy-mm-dd` string. */
export function addDays(day: string, n: number): string {
  const d = new Date(`${day}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + n)
  return d.toISOString().slice(0, 10)
}

export const RANGE_PRESETS = ['24h', '7d', '30d', '90d'] as const
export type RangePreset = (typeof RANGE_PRESETS)[number]

const PRESET_MS: Record<RangePreset, number> = {
  '24h': 24 * 3600_000,
  '7d': 7 * 24 * 3600_000,
  '30d': 30 * 24 * 3600_000,
  '90d': 90 * 24 * 3600_000,
}

/** `[from, to)` ending now, rounded down to the minute so query keys stay stable. */
export function presetRange(preset: RangePreset, nowMs: number): { from: string; to: string } {
  const to = Math.floor(nowMs / 60_000) * 60_000 + 60_000
  return { from: new Date(to - PRESET_MS[preset]).toISOString(), to: new Date(to).toISOString() }
}
