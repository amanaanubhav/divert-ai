import type { IncidentStatus, IncidentType, ServiceType } from '../api/types'

export function formatDistance(m: number | null | undefined): string {
  if (m == null || !Number.isFinite(m)) return '–'
  if (m < 1000) return `${Math.round(m)} m`
  return `${(m / 1000).toFixed(m < 10_000 ? 2 : 1)} km`
}

/** `73.4` -> `1:13`, `3725` -> `1:02:05`. */
export function formatDuration(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return '–'
  const total = Math.max(0, Math.round(s))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const sec = total % 60
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m)
  return `${h > 0 ? `${h}:` : ''}${mm}:${String(sec).padStart(2, '0')}`
}

/** Short human duration: `45 s`, `3.2 min`, `1.4 h`. */
export function formatSeconds(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return '–'
  if (Math.abs(s) < 90) return `${Math.round(s)} s`
  if (Math.abs(s) < 5400) return `${(s / 60).toFixed(1)} min`
  return `${(s / 3600).toFixed(1)} h`
}

export function formatSpeed(mps: number | null | undefined): string {
  if (mps == null || !Number.isFinite(mps)) return '–'
  return `${Math.round(mps * 3.6)} km/h`
}

export function formatPercent(x: number | null | undefined, digits = 1): string {
  if (x == null || !Number.isFinite(x)) return '–'
  return `${(x * 100).toFixed(digits)}%`
}

const dtfCache = new Map<string, Intl.DateTimeFormat>()
function dtf(timeZone: string, opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${timeZone}|${JSON.stringify(opts)}`
  let f = dtfCache.get(key)
  if (!f) {
    f = new Intl.DateTimeFormat('en-IN', { timeZone, ...opts })
    dtfCache.set(key, f)
  }
  return f
}

/** Clock time in the city's zone, e.g. `14:05:09`. */
export function formatClock(iso: string | null | undefined, timeZone: string): string {
  if (!iso) return '--:--:--'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '--:--:--'
  return dtf(timeZone, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(d)
}

/** Date + time in the city's zone, e.g. `17 Nov 2026, 14:05`. */
export function formatDateTime(iso: string | null | undefined, timeZone: string): string {
  if (!iso) return '–'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '–'
  return dtf(timeZone, {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(d)
}

export function formatLatLng(p: readonly [number, number]): string {
  return `${p[0].toFixed(5)}, ${p[1].toFixed(5)}`
}

export const TYPE_LABEL: Readonly<Record<IncidentType, string>> = {
  MEDICAL: 'Medical',
  FIRE: 'Fire',
  CRIME: 'Crime',
}

export const SERVICE_LABEL: Readonly<Record<ServiceType, string>> = {
  AMBULANCE: 'Ambulance',
  FIRE_ENGINE: 'Fire engine',
  POLICE: 'Police',
}

export const STATUS_LABEL: Readonly<Record<IncidentStatus, string>> = {
  OPEN: 'Open',
  DISPATCHED: 'Dispatched',
  EN_ROUTE: 'En route',
  ON_SCENE: 'On scene',
  RESOLVED: 'Resolved',
  CANCELLED: 'Cancelled',
  FAILED: 'Failed',
}

/** Seconds elapsed between two ISO times (`null` if either is missing). */
export function secondsBetween(
  a: string | null | undefined,
  b: string | null | undefined,
): number | null {
  if (!a || !b) return null
  const d = (new Date(b).getTime() - new Date(a).getTime()) / 1000
  return Number.isFinite(d) ? d : null
}
