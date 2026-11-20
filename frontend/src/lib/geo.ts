/**
 * Geodesy helpers. Same model as the backend (`divert.domain.geo`): a spherical Earth with
 * the IUGG mean radius, haversine distances, positions by cumulative haversine distance.
 * All points are `[lat, lng]` in decimal degrees.
 */
import type { BBox, LatLng } from '../api/types'

export const EARTH_RADIUS_M = 6_371_008.8

const toRad = (deg: number) => (deg * Math.PI) / 180
const toDeg = (rad: number) => (rad * 180) / Math.PI

/** Great-circle distance in metres. */
export function haversine(a: LatLng, b: LatLng): number {
  const dLat = toRad(b[0] - a[0])
  const dLng = toRad(b[1] - a[1])
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLng / 2) ** 2
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)))
}

/** Initial bearing from `a` to `b`, degrees clockwise from north in [0, 360). */
export function bearing(a: LatLng, b: LatLng): number {
  const φ1 = toRad(a[0])
  const φ2 = toRad(b[0])
  const Δλ = toRad(b[1] - a[1])
  const y = Math.sin(Δλ) * Math.cos(φ2)
  const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ)
  return (toDeg(Math.atan2(y, x)) + 360) % 360
}

/** Cumulative distance at every vertex (`[0, d01, d01+d12, ...]`). */
export function cumulativeDistances(points: readonly LatLng[]): number[] {
  const out: number[] = new Array<number>(points.length)
  let total = 0
  for (let i = 0; i < points.length; i++) {
    if (i > 0) total += haversine(points[i - 1] as LatLng, points[i] as LatLng)
    out[i] = total
  }
  return out
}

export interface PolylinePosition {
  pos: LatLng
  /** Heading of the segment the position lies on. */
  heading: number
  /** Index of the segment start vertex. */
  index: number
}

/** Index `i` of the segment `[i, i+1]` that contains `along` (binary search). */
function segmentIndex(cum: readonly number[], along: number): number {
  let lo = 0
  let hi = cum.length - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if ((cum[mid] as number) <= along) lo = mid
    else hi = mid
  }
  return lo
}

/**
 * Point at `along` metres from the start, linearly interpolated inside its segment.
 * Clamped to the ends. `cum` must be `cumulativeDistances(points)`.
 */
export function interpolateAlong(
  points: readonly LatLng[],
  cum: readonly number[],
  along: number,
): PolylinePosition {
  const n = points.length
  if (n === 0) throw new Error('empty polyline')
  if (n === 1) return { pos: points[0] as LatLng, heading: 0, index: 0 }
  const total = cum[n - 1] as number
  const d = Math.max(0, Math.min(total, along))
  const i = Math.min(segmentIndex(cum, d), n - 2)
  const a = points[i] as LatLng
  const b = points[i + 1] as LatLng
  const segLen = (cum[i + 1] as number) - (cum[i] as number)
  const t = segLen > 0 ? (d - (cum[i] as number)) / segLen : 0
  return {
    pos: [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t],
    heading: segLen > 0 ? bearing(a, b) : headingNear(points, i),
    index: i,
  }
}

function headingNear(points: readonly LatLng[], i: number): number {
  for (let j = i; j < points.length - 1; j++) {
    const a = points[j] as LatLng
    const b = points[j + 1] as LatLng
    if (a[0] !== b[0] || a[1] !== b[1]) return bearing(a, b)
  }
  return 0
}

/** Split a polyline at `along`: `[travelled, remaining]`, both containing the split point. */
export function splitAt(
  points: readonly LatLng[],
  cum: readonly number[],
  along: number,
): [LatLng[], LatLng[]] {
  if (points.length < 2) return [[...points], [...points]]
  const { pos, index } = interpolateAlong(points, cum, along)
  return [
    [...points.slice(0, index + 1), pos],
    [pos, ...points.slice(index + 1)],
  ]
}

/** Projection of `p` onto the polyline: distance along it and the perpendicular offset. */
export function projectOnto(
  points: readonly LatLng[],
  cum: readonly number[],
  p: LatLng,
): { along: number; offset: number } {
  let best = { along: 0, offset: Number.POSITIVE_INFINITY }
  if (points.length === 0) return best
  // Local equirectangular frame around p: accurate to << 1 m at city scale.
  const kx = Math.cos(toRad(p[0])) * toRad(1) * EARTH_RADIUS_M
  const ky = toRad(1) * EARTH_RADIUS_M
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i] as LatLng
    const b = points[i + 1] as LatLng
    const ax = (a[1] - p[1]) * kx
    const ay = (a[0] - p[0]) * ky
    const bx = (b[1] - p[1]) * kx
    const by = (b[0] - p[0]) * ky
    const dx = bx - ax
    const dy = by - ay
    const len2 = dx * dx + dy * dy
    const t = len2 > 0 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2)) : 0
    const qx = ax + t * dx
    const qy = ay + t * dy
    const offset = Math.hypot(qx, qy)
    if (offset < best.offset) {
      const segLen = (cum[i + 1] as number) - (cum[i] as number)
      best = { along: (cum[i] as number) + t * segLen, offset }
    }
  }
  return best
}

/** Is the point inside `[south, west, north, east]` (inclusive)? */
export function insideBBox(p: LatLng, bbox: BBox): boolean {
  const [s, w, n, e] = bbox
  return p[0] >= s && p[0] <= n && p[1] >= w && p[1] <= e
}

/** Nearest item by haversine distance (`null` for an empty list). */
export function nearest<T>(
  p: LatLng,
  items: readonly T[],
  locate: (item: T) => LatLng,
): { item: T; distance_m: number } | null {
  let best: { item: T; distance_m: number } | null = null
  for (const item of items) {
    const d = haversine(p, locate(item))
    if (!best || d < best.distance_m) best = { item, distance_m: d }
  }
  return best
}
