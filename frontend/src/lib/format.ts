import type {
  DestinationKind,
  DispatchStatus,
  IncidentStatus,
  IncidentType,
  JunctionState,
  LegEndReason,
  LegKind,
  Movement,
  PreemptOutcome,
  ServiceType,
  UnitPhase,
} from '../api/types'

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
  TRANSPORTING: 'Transporting',
  AT_HOSPITAL: 'At hospital',
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

/** `+2.2%` / `-0.5%` from a percentage value (not a fraction). */
export function formatSignedPct(pct: number | null | undefined, digits = 1): string {
  if (pct == null || !Number.isFinite(pct)) return '–'
  const r = Number(pct.toFixed(digits))
  return `${r > 0 ? '+' : ''}${r.toFixed(digits)}%`
}

/** Hospital label with its OSM emergency-department tag. */
export function hospitalLabel(h: {
  name: string | null
  id?: string
  station_id?: string
}): string {
  return h.name ?? h.id ?? h.station_id ?? 'unnamed hospital'
}

/** `yes` -> "Emergency department", `no` -> "No emergency department", null -> "ED not tagged". */
export function edLabel(tag: string | null | undefined): string {
  if (tag === 'yes') return 'Emergency department'
  if (tag === 'no') return 'No emergency department'
  return 'ED not tagged in OSM'
}

/** Unit status while on a dispatch (the phase chip). */
export const PHASE_LABEL: Readonly<Record<UnitPhase, string>> = {
  DISPATCHED: 'Responding',
  ON_SCENE: 'On scene',
  TRANSPORTING: 'Transporting',
  AT_HOSPITAL: 'At hospital',
  RETURNING: 'Returning',
}

export const DISPATCH_STATUS_LABEL: Readonly<Record<DispatchStatus, string>> = {
  ...PHASE_LABEL,
  COMPLETED: 'Completed',
  FAILED: 'Failed',
}

export const LEG_LABEL: Readonly<Record<LegKind, string>> = {
  RESPONSE: 'Response',
  TRANSPORT: 'Transport',
  RETURN: 'Return',
}

export const DEST_KIND_LABEL: Readonly<Record<DestinationKind, string>> = {
  SCENE: 'scene',
  HOSPITAL: 'hospital',
  STATION: 'station',
}

export const LEG_END_LABEL: Readonly<Record<LegEndReason, string>> = {
  ARRIVED: 'Arrived',
  CANCELLED: 'Cancelled',
  FAILED: 'Failed',
  SUPERSEDED: 'Not driven (superseded)',
}

export const MOVEMENT_LABEL: Readonly<Record<Movement, string>> = {
  STRAIGHT: 'Straight on',
  SLIGHT_LEFT: 'Slight left',
  LEFT: 'Left',
  SHARP_LEFT: 'Sharp left',
  SLIGHT_RIGHT: 'Slight right',
  RIGHT: 'Right',
  SHARP_RIGHT: 'Sharp right',
  U_TURN: 'U-turn',
  ROUNDABOUT: 'Roundabout',
}

/** Junction states (design 4.3) as operators read them. */
export const JUNCTION_STATE_LABEL: Readonly<Record<JunctionState, string>> = {
  NORMAL: 'Normal',
  TRANSITIONING: 'Transitioning',
  PREEMPTED: 'Pre-empted',
  QUEUED: 'Queued',
  RECOVERING: 'Recovering',
}

export const JUNCTION_STATE_HINT: Readonly<Record<JunctionState, string>> = {
  NORMAL: 'running its fixed plan',
  TRANSITIONING: 'clearing the current phase towards green for the vehicle',
  PREEMPTED: 'green held for the vehicle',
  QUEUED: 'green held; another vehicle waits for its turn',
  RECOVERING: 'released; returning to its plan',
}

export const OUTCOME_LABEL: Readonly<Record<PreemptOutcome, string>> = {
  CLEARED: 'Cleared',
  TIMEOUT: 'Timed out',
  CANCELLED: 'Cancelled',
  REROUTED: 'Re-routed',
}

/** Conflict priority rank (design 4.4). */
export const RANK_LABEL: Readonly<Record<number, string>> = {
  0: 'ambulance with patient',
  1: 'ambulance',
  2: 'fire engine',
  3: 'police',
}
