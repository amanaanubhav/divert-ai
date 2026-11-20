import { describe, expect, it } from 'vitest'
import type { LatLng } from '../api/types'
import {
  bearing,
  cumulativeDistances,
  EARTH_RADIUS_M,
  haversine,
  insideBBox,
  interpolateAlong,
  nearest,
  projectOnto,
  splitAt,
} from './geo'

// Reference values computed with the backend model (divert.domain.geo, R = 6371008.8 m),
// so the client draws exactly what the server simulates.
const DELHI: LatLng = [28.6139, 77.209]
const KOLKATA: LatLng = [22.5726, 88.3639]
const BENGALURU: LatLng = [12.9716, 77.5946]

describe('haversine', () => {
  it('uses the IUGG mean radius: 1 degree of latitude', () => {
    expect(EARTH_RADIUS_M).toBe(6_371_008.8)
    expect(haversine([0, 0], [1, 0])).toBeCloseTo(111_195.0802335329, 6)
  })

  it('matches the backend for city-to-city distances', () => {
    expect(haversine(DELHI, KOLKATA)).toBeCloseTo(1_303_835.2695184073, 4)
    expect(haversine(DELHI, BENGALURU)).toBeCloseTo(1_739_804.5050131553, 4)
  })

  it('is symmetric and zero for identical points', () => {
    expect(haversine(KOLKATA, DELHI)).toBeCloseTo(haversine(DELHI, KOLKATA), 6)
    expect(haversine(DELHI, DELHI)).toBe(0)
  })
})

describe('bearing', () => {
  it('cardinal directions', () => {
    expect(bearing([0, 0], [1, 0])).toBeCloseTo(0, 9)
    expect(bearing([0, 0], [0, 1])).toBeCloseTo(90, 9)
    expect(bearing([0, 0], [-1, 0])).toBeCloseTo(180, 9)
    expect(bearing([0, 0], [0, -1])).toBeCloseTo(270, 9)
  })

  it('matches the backend initial bearing', () => {
    expect(bearing(DELHI, KOLKATA)).toBeCloseTo(118.47429555824971, 9)
  })
})

describe('polyline helpers', () => {
  const pts: LatLng[] = [
    [28.6, 77.2],
    [28.61, 77.2],
    [28.61, 77.21],
  ]
  const cum = cumulativeDistances(pts)

  it('cumulative distances by haversine', () => {
    expect(cum[0]).toBe(0)
    expect(cum[1]).toBeCloseTo(1111.9508023352598, 6)
    expect(cum[2]).toBeCloseTo(2088.1317604463493, 6)
  })

  it('interpolates by distance like the backend', () => {
    const p = interpolateAlong(pts, cum, 1500)
    expect(p.index).toBe(1)
    expect(p.pos[0]).toBeCloseTo(28.61, 9)
    expect(p.pos[1]).toBeCloseTo(77.2039751768813, 9)
    expect(p.heading).toBeCloseTo(bearing(pts[1]!, pts[2]!), 9)
  })

  it('clamps to the ends', () => {
    expect(interpolateAlong(pts, cum, -50).pos).toEqual(pts[0])
    const end = interpolateAlong(pts, cum, 1e9).pos
    expect(end[0]).toBeCloseTo(28.61, 9)
    expect(end[1]).toBeCloseTo(77.21, 9)
  })

  it('splits into travelled + remaining sharing the split point', () => {
    const [t, r] = splitAt(pts, cum, 500)
    expect(t).toHaveLength(2)
    expect(r).toHaveLength(3)
    expect(t[t.length - 1]).toEqual(r[0])
    expect(haversine(pts[0]!, t[1]!)).toBeCloseTo(500, 3)
  })

  it('projects a point onto the polyline (distance along + offset)', () => {
    // 10 m east of the first segment, 300 m north of its start
    const north300 = interpolateAlong(pts, cum, 300).pos
    const p: LatLng = [
      north300[0],
      north300[1] + 10 / (111_195.08 * Math.cos((north300[0] * Math.PI) / 180)),
    ]
    const proj = projectOnto(pts, cum, p)
    expect(proj.along).toBeCloseTo(300, 0)
    expect(proj.offset).toBeCloseTo(10, 1)
  })
})

describe('bbox + nearest', () => {
  it('bbox is [south, west, north, east] inclusive', () => {
    const bbox = [28.4, 76.84, 28.88, 77.35] as const
    expect(insideBBox(DELHI, bbox)).toBe(true)
    expect(insideBBox([28.4, 76.84], bbox)).toBe(true)
    expect(insideBBox(KOLKATA, bbox)).toBe(false)
  })

  it('finds the nearest item by haversine distance', () => {
    const items = [
      { id: 'k', at: KOLKATA },
      { id: 'b', at: BENGALURU },
    ]
    const n = nearest(DELHI, items, (i) => i.at)
    expect(n?.item.id).toBe('k')
    expect(n?.distance_m).toBeCloseTo(1_303_835.27, 1)
    expect(nearest(DELHI, [], (i: { at: LatLng }) => i.at)).toBeNull()
  })
})
