import type {
  DispatchRecord,
  IncidentDetail,
  LatLng,
  LegKind,
  LegRecord,
  PreemptionRecord,
  TrackPoint,
} from '../../api/types'
import { bearing, cumulativeDistances, interpolateAlong } from '../../lib/geo'

/** Route key of one leg: `along_m` of a track point is measured along that leg. */
export const legKey = (dispatchId: string, seq: number | null | undefined) =>
  `${dispatchId}:${seq ?? 0}`

export interface Replay {
  points: TrackPoint[]
  /** Milliseconds since the first sample, per point. */
  times: number[]
  durationMs: number
  /** Leg routes by `legKey`. */
  routes: Map<string, { route: LatLng[]; cum: number[] }>
}

export interface ReplayFrame {
  pos: LatLng
  heading: number
  /** Index of the sample at or before the frame. */
  index: number
  speed_mps: number | null
  dispatchId: string
  legSeq: number
}

/** One leg on the replay time axis (from its first to its last sample). */
export interface LegSpan {
  dispatchId: string
  seq: number
  /** null: no leg record (an older server). */
  kind: LegKind | null
  startMs: number
  endMs: number
}

/**
 * Sort the recorded samples and index every leg route for road-following replay. Without
 * `legs` (an older server) the dispatch route is the RESPONSE leg (seq 0).
 */
export function prepareReplay(
  points: readonly TrackPoint[],
  dispatches: readonly DispatchRecord[],
  legs: readonly LegRecord[] = [],
): Replay | null {
  if (points.length === 0) return null
  const sorted = [...points].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts))
  const t0 = Date.parse((sorted[0] as TrackPoint).ts)
  const times = sorted.map((p) => Date.parse(p.ts) - t0)
  const routes = new Map<string, { route: LatLng[]; cum: number[] }>()
  for (const d of dispatches) {
    if (d.route.length > 1)
      routes.set(legKey(d.id, 0), { route: d.route, cum: cumulativeDistances(d.route) })
  }
  for (const l of legs) {
    if (l.route.length > 1)
      routes.set(legKey(l.dispatch_id, l.seq), {
        route: l.route,
        cum: cumulativeDistances(l.route),
      })
  }
  return { points: sorted, times, durationMs: times[times.length - 1] ?? 0, routes }
}

/**
 * Where the vehicle was `tMs` after the first sample. Between two samples of the same leg
 * with `along_m`, the position moves along that leg's route (follows the road); otherwise
 * (a leg change, a missing route) it is interpolated linearly between the two samples.
 */
export function frameAt(r: Replay, tMs: number): ReplayFrame {
  const n = r.points.length
  const t = Math.max(0, Math.min(r.durationMs, tMs))
  let i = 0
  while (i < n - 1 && (r.times[i + 1] as number) <= t) i++
  const a = r.points[i] as TrackPoint
  const b = r.points[Math.min(i + 1, n - 1)] as TrackPoint
  const ta = r.times[i] as number
  const tb = r.times[Math.min(i + 1, n - 1)] as number
  const f = tb > ta ? (t - ta) / (tb - ta) : 0
  const speed = a.speed_mps
  const sameLeg = a.dispatch_id === b.dispatch_id && (a.leg_seq ?? 0) === (b.leg_seq ?? 0)
  const route = sameLeg ? r.routes.get(legKey(a.dispatch_id, a.leg_seq)) : undefined
  const base = { index: i, speed_mps: speed, dispatchId: a.dispatch_id, legSeq: a.leg_seq ?? 0 }
  if (route && a.along_m != null && b.along_m != null) {
    const along = a.along_m + (b.along_m - a.along_m) * f
    const p = interpolateAlong(route.route, route.cum, along)
    return { pos: p.pos, heading: p.heading, ...base }
  }
  const pos: LatLng = [a.pos[0] + (b.pos[0] - a.pos[0]) * f, a.pos[1] + (b.pos[1] - a.pos[1]) * f]
  const heading = a.pos[0] === b.pos[0] && a.pos[1] === b.pos[1] ? 0 : bearing(a.pos, b.pos)
  return { pos, heading, ...base }
}

/** The legs on the replay time axis, in order (a leg without samples is left out). */
export function legSpans(r: Replay, legs: readonly LegRecord[]): LegSpan[] {
  const kindOf = new Map(legs.map((l) => [legKey(l.dispatch_id, l.seq), l.kind] as const))
  const spans: LegSpan[] = []
  r.points.forEach((p, i) => {
    const key = legKey(p.dispatch_id, p.leg_seq)
    const t = r.times[i] as number
    const last = spans[spans.length - 1]
    if (last && legKey(last.dispatchId, last.seq) === key) {
      last.endMs = t
      return
    }
    if (last) last.endMs = Math.max(last.endMs, t) // the gap up to the next leg is the dwell
    spans.push({
      dispatchId: p.dispatch_id,
      seq: p.leg_seq ?? 0,
      kind: kindOf.get(key) ?? ((p.leg_seq ?? 0) === 0 ? 'RESPONSE' : null),
      startMs: t,
      endMs: t,
    })
  })
  return spans
}

/** The track samples of each leg, for drawing the travelled path in that leg's style. */
export function travelledByLeg(
  r: Replay,
  frame: ReplayFrame,
): { key: string; dispatchId: string; seq: number; pts: LatLng[] }[] {
  const out: { key: string; dispatchId: string; seq: number; pts: LatLng[] }[] = []
  for (let i = 0; i <= frame.index && i < r.points.length; i++) {
    const p = r.points[i] as TrackPoint
    const key = legKey(p.dispatch_id, p.leg_seq)
    let cur = out[out.length - 1]
    if (cur?.key !== key) {
      cur = { key, dispatchId: p.dispatch_id, seq: p.leg_seq ?? 0, pts: [] }
      out.push(cur)
    }
    cur.pts.push(p.pos)
  }
  const last = out[out.length - 1]
  if (last?.key === legKey(frame.dispatchId, frame.legSeq)) last.pts.push(frame.pos)
  return out
}

/** The legs to draw: the recorded legs, or the dispatch routes of an older server. */
export function drawableLegs(detail: IncidentDetail): LegRecord[] {
  if (detail.legs && detail.legs.length > 0) return detail.legs.filter((l) => l.route.length > 1)
  return detail.dispatches.map((d) => ({
    dispatch_id: d.id,
    seq: 0,
    kind: 'RESPONSE',
    emergency: true,
    origin: d.route[0] ?? d.station.location,
    destination: { kind: 'SCENE', station_id: null, name: null, location: detail.location },
    route: d.route,
    distance_m: d.distance_m,
    predicted_eta_s: d.predicted_eta_s,
    actual_travel_s: d.actual_travel_s,
    eta_error_pct: null,
    eta_model: d.eta_model,
    planned_at: d.dispatched_at,
    started_at: d.dispatched_at,
    ended_at: d.arrived_at,
    end_reason: null,
    reroutes: 0,
    started_trip_s: null,
    ended_trip_s: null,
    corridor: d.corridor,
    route_choice: d.route_choice ?? null,
    hospital_choice: null,
  }))
}

/** The pre-emption of a corridor junction on a given leg (older records: leg 0). */
export function preemptionAt(
  pre: readonly PreemptionRecord[],
  dispatchId: string,
  seq: number,
  signalId: string,
): PreemptionRecord | undefined {
  return pre.find(
    (p) => p.dispatch_id === dispatchId && p.signal_id === signalId && (p.leg_seq ?? 0) === seq,
  )
}
