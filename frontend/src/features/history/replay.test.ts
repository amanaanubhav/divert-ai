import { describe, expect, it } from 'vitest'
import type { DispatchRecord, TrackPoint } from '../../api/types'
import { haversine } from '../../lib/geo'
import { ROUTE } from '../../test/fixtures'
import { medicalDetail } from '../../test/fixtures'
import { drawableLegs, frameAt, legSpans, prepareReplay, travelledByLeg } from './replay'

const dispatch = { id: 'd1', route: ROUTE } as unknown as DispatchRecord

const points: TrackPoint[] = [
  {
    dispatch_id: 'd1',
    ts: '2026-11-17T06:30:10Z',
    pos: [28.61, 77.2],
    speed_mps: 10,
    along_m: 1111.95,
  },
  { dispatch_id: 'd1', ts: '2026-11-17T06:30:00Z', pos: [28.6, 77.2], speed_mps: 0, along_m: 0 },
  {
    dispatch_id: 'd1',
    ts: '2026-11-17T06:30:20Z',
    pos: [28.61, 77.21],
    speed_mps: 0,
    along_m: 2088.13,
  },
]

describe('track replay', () => {
  it('sorts samples and measures the duration', () => {
    const r = prepareReplay(points, [dispatch])!
    expect(r.times).toEqual([0, 10_000, 20_000])
    expect(r.durationMs).toBe(20_000)
    expect(prepareReplay([], [])).toBeNull()
  })

  it('moves along the recorded route between samples (follows the road)', () => {
    const r = prepareReplay(points, [dispatch])!
    // halfway between sample 2 (along 1111.95, the corner) and sample 3 (end):
    const f = frameAt(r, 15_000)
    expect(f.index).toBe(1)
    const along = 1111.95 + (2088.13 - 1111.95) / 2
    // on the second (eastbound) segment of the route, not the straight chord
    expect(f.pos[0]).toBeCloseTo(28.61, 6)
    expect(haversine([28.61, 77.2], f.pos)).toBeCloseTo(along - 1111.95, 0)
    expect(f.heading).toBeCloseTo(90, 0)
  })

  it('falls back to straight interpolation without along_m and clamps to the ends', () => {
    const noAlong = points.map((p) => ({ ...p, along_m: null }))
    const r = prepareReplay(noAlong, [])!
    const f = frameAt(r, 5_000)
    expect(f.pos[0]).toBeCloseTo(28.605, 9)
    expect(frameAt(r, -1).pos).toEqual([28.6, 77.2])
    expect(frameAt(r, 1e9).pos).toEqual([28.61, 77.21])
  })

  it('replays across legs: each leg follows its own route, along_m restarts per leg', () => {
    const d = medicalDetail()
    const legs = drawableLegs(d)
    const transportRoute = legs[1]!.route
    const pts: TrackPoint[] = [
      {
        dispatch_id: 'd9',
        ts: '2026-11-17T06:30:00Z',
        pos: [28.6, 77.2],
        speed_mps: 0,
        along_m: 0,
        leg_seq: 0,
      },
      {
        dispatch_id: 'd9',
        ts: '2026-11-17T06:30:20Z',
        pos: [28.61, 77.21],
        speed_mps: 0,
        along_m: 2088.13,
        leg_seq: 0,
      },
      // dwell on scene, then the TRANSPORT leg starts at along 0 of its own route
      {
        dispatch_id: 'd9',
        ts: '2026-11-17T06:31:00Z',
        pos: transportRoute[0]!,
        speed_mps: 0,
        along_m: 0,
        leg_seq: 1,
      },
      {
        dispatch_id: 'd9',
        ts: '2026-11-17T06:31:20Z',
        pos: [28.61, 77.21],
        speed_mps: 9,
        along_m: 1111.95,
        leg_seq: 1,
      },
    ]
    const r = prepareReplay(pts, d.dispatches, legs)!
    expect([...r.routes.keys()]).toEqual(['d9:0', 'd9:1', 'd9:2'])
    const f = frameAt(r, 70_000) // halfway through the first TRANSPORT samples
    expect(f.legSeq).toBe(1)
    expect(f.index).toBe(2)
    expect(f.pos[0]).toBeCloseTo(28.605, 6) // along the TRANSPORT route (its first segment)
    // between the last RESPONSE and the first TRANSPORT sample: straight, never the wrong route
    expect(frameAt(r, 40_000).legSeq).toBe(0)
    const spans = legSpans(r, legs)
    expect(spans.map((s) => [s.kind, s.startMs, s.endMs])).toEqual([
      ['RESPONSE', 0, 60_000],
      ['TRANSPORT', 60_000, 80_000],
    ])
    const trav = travelledByLeg(r, f)
    expect(trav.map((t) => [t.key, t.pts.length])).toEqual([
      ['d9:0', 2],
      ['d9:1', 2],
    ])
  })
})
