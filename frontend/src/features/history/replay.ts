import type { DispatchRecord, LatLng, TrackPoint } from '../../api/types'
import { bearing, cumulativeDistances, interpolateAlong } from '../../lib/geo'

export interface Replay {
  points: TrackPoint[]
  /** Milliseconds since the first sample, per point. */
  times: number[]
  durationMs: number
  routes: Map<string, { route: LatLng[]; cum: number[] }>
}

export interface ReplayFrame {
  pos: LatLng
  heading: number
  /** Index of the sample at or before the frame. */
  index: number
  speed_mps: number | null
}

/** Sort the recorded samples and index the dispatch routes for road-following replay. */
export function prepareReplay(
  points: readonly TrackPoint[],
  dispatches: readonly DispatchRecord[],
): Replay | null {
  if (points.length === 0) return null
  const sorted = [...points].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts))
  const t0 = Date.parse((sorted[0] as TrackPoint).ts)
  const times = sorted.map((p) => Date.parse(p.ts) - t0)
  const routes = new Map<string, { route: LatLng[]; cum: number[] }>()
  for (const d of dispatches) {
    if (d.route.length > 1) routes.set(d.id, { route: d.route, cum: cumulativeDistances(d.route) })
  }
  return { points: sorted, times, durationMs: times[times.length - 1] ?? 0, routes }
}

/**
 * Where the vehicle was `tMs` after the first sample. Between two samples of the same
 * dispatch with `along_m`, the position moves along the recorded route (follows the road);
 * otherwise it is interpolated linearly between the two samples.
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
  const route = a.dispatch_id === b.dispatch_id ? r.routes.get(a.dispatch_id) : undefined
  if (route && a.along_m != null && b.along_m != null) {
    const along = a.along_m + (b.along_m - a.along_m) * f
    const p = interpolateAlong(route.route, route.cum, along)
    return { pos: p.pos, heading: p.heading, index: i, speed_mps: speed }
  }
  const pos: LatLng = [a.pos[0] + (b.pos[0] - a.pos[0]) * f, a.pos[1] + (b.pos[1] - a.pos[1]) * f]
  const heading = a.pos[0] === b.pos[0] && a.pos[1] === b.pos[1] ? 0 : bearing(a.pos, b.pos)
  return { pos, heading, index: i, speed_mps: speed }
}
