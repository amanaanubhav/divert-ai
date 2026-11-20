import { beforeEach, describe, expect, it } from 'vitest'
import type { ActiveSignal } from '../api/types'
import { tick, vehicleDoc } from '../test/fixtures'
import { haversine } from '../lib/geo'
import { motionAlong, nextSignal, useLiveStore, vehiclePose, vehiclesMissingRoute } from './live'

const store = () => useLiveStore.getState()

const SIGNAL: ActiveSignal = {
  signal_id: 'J10',
  state: 'PREEMPTED',
  pos: [28.61, 77.2],
  vehicle_id: 'PCR-Pw1-1',
  service: 'POLICE',
  lane: 'L',
  approach_bearing: 0,
  eta_s: 12.3,
  claimants: [{ vehicle_id: 'PCR-Pw1-1', service: 'POLICE', eta_s: 12.3, state: 'PREEMPTED' }],
}

function tickVehicle(along: number, extra: Record<string, unknown> = {}) {
  const v = vehicleDoc()
  return {
    id: v.id,
    dispatch_id: v.dispatch_id,
    incident_id: v.incident_id,
    service: v.service,
    status: v.status,
    pos: v.pos,
    heading: 0,
    speed_mps: 10,
    eta_s: 100,
    along_m: along,
    distance_m: v.distance_m,
    preempted_passed: 0,
    ...extra,
  } as const
}

describe('live store', () => {
  beforeEach(() => store().reset('delhi'))

  it('snapshot replaces vehicles (with routes) and signals', () => {
    store().apply(
      {
        type: 'snapshot',
        v: 1,
        city: 'delhi',
        tick: 7,
        sim_ts: null,
        vehicles: [vehicleDoc()],
        signals: [SIGNAL],
      },
      0,
    )
    const s = store()
    expect(s.tick).toBe(7)
    expect(Object.keys(s.vehicles)).toEqual(['PCR-Pw1-1'])
    expect(s.vehicles['PCR-Pw1-1']?.cum?.[2]).toBeCloseTo(2088.13, 1)
    expect(s.signals.J10?.state).toBe('PREEMPTED')
  })

  it('ignores messages for another city', () => {
    store().apply(
      {
        type: 'snapshot',
        v: 1,
        city: 'kolkata',
        tick: 1,
        sim_ts: null,
        vehicles: [vehicleDoc()],
        signals: [],
      },
      0,
    )
    expect(store().vehicles).toEqual({})
  })

  it('interpolates smoothly between ticks along the route by haversine distance', () => {
    store().apply(
      {
        type: 'snapshot',
        v: 1,
        city: 'delhi',
        tick: 1,
        sim_ts: null,
        vehicles: [vehicleDoc()],
        signals: [],
      },
      0,
    )
    store().apply(tick(2, [tickVehicle(100)]), 1000)
    store().apply(tick(3, [tickVehicle(200)]), 1500)
    const v = store().vehicles['PCR-Pw1-1']!
    // starts where it was drawn, ends at the new along, linear in between
    expect(motionAlong(v.motion, 1500)).toBeCloseTo(100, 6)
    expect(motionAlong(v.motion, 1500 + v.motion.durMs / 2)).toBeCloseTo(150, 6)
    expect(motionAlong(v.motion, 1e9)).toBeCloseTo(200, 6)
    const pose = vehiclePose(v, 1e9)
    expect(haversine(pose.pos, [28.6, 77.2])).toBeCloseTo(200, 3) // on the first (northbound) segment
    expect(pose.heading).toBeCloseTo(0, 6)
  })

  it('snaps instead of animating backwards (restore after a failover)', () => {
    store().apply(
      {
        type: 'snapshot',
        v: 1,
        city: 'delhi',
        tick: 1,
        sim_ts: null,
        vehicles: [vehicleDoc({ along_m: 500 })],
        signals: [],
      },
      0,
    )
    store().apply(tick(2, [tickVehicle(300)]), 500)
    const v = store().vehicles['PCR-Pw1-1']!
    expect(v.motion.fromAlong).toBe(300)
    expect(v.motion.toAlong).toBe(300)
  })

  it('tick replaces the complete active-signal set and drops vehicles that are gone', () => {
    store().apply(
      {
        type: 'snapshot',
        v: 1,
        city: 'delhi',
        tick: 1,
        sim_ts: null,
        vehicles: [vehicleDoc()],
        signals: [SIGNAL],
      },
      0,
    )
    store().apply(tick(2, [], []), 500)
    expect(store().signals).toEqual({})
    expect(store().vehicles).toEqual({})
  })

  it('vehicle_added carries route, corridor and static facts; removed deletes', () => {
    const doc = vehicleDoc()
    store().apply(
      {
        type: 'vehicle_added',
        v: 1,
        city: 'delhi',
        vehicle: {
          ...tickVehicle(0),
          station_id: 'Pw1',
          incident_type: 'CRIME',
          predicted_eta_s: 130,
        },
        route: doc.route!,
        corridor: doc.corridor!,
        destination: doc.destination,
      },
      0,
    )
    const v = store().vehicles['PCR-Pw1-1']!
    expect(v.station_id).toBe('Pw1')
    expect(v.route).toHaveLength(3)
    expect(vehiclesMissingRoute(store())).toEqual([])
    expect(store().feed[0]?.kind).toBe('added')

    store().apply(
      {
        type: 'vehicle_removed',
        v: 1,
        city: 'delhi',
        vehicle_id: 'PCR-Pw1-1',
        dispatch_id: 'd1',
        incident_id: 'i1',
        reason: 'cleared',
      },
      10,
    )
    expect(store().vehicles).toEqual({})
    expect(store().feed[0]?.tone).toBe('good')
  })

  it('a vehicle first seen in a tick is flagged as missing its route, then merged', () => {
    store().apply(tick(1, [tickVehicle(50)]), 0)
    expect(vehiclesMissingRoute(store())).toEqual(['PCR-Pw1-1'])
    store().mergeRoutes([vehicleDoc()])
    expect(vehiclesMissingRoute(store())).toEqual([])
    expect(store().vehicles['PCR-Pw1-1']?.station_id).toBe('Pw1')
  })

  it('incident_updated records the live status', () => {
    store().apply(
      {
        type: 'incident_updated',
        v: 1,
        city: 'delhi',
        incident_id: 'i1',
        dispatch_id: 'd1',
        vehicle_id: 'v',
        status: 'ON_SCENE',
        ts: '2026-11-17T06:31:00+00:00',
        travel_s: 75,
      },
      0,
    )
    expect(store().incidentStatus.i1).toMatchObject({ status: 'ON_SCENE', travel_s: 75 })
  })

  it('nextSignal finds the first corridor junction ahead', () => {
    const v = vehicleDoc({ along_m: 1000 })
    expect(nextSignal(v)?.entry.signal_id).toBe('J10')
    expect(nextSignal(v)?.distance_m).toBeCloseTo(111.95, 2)
    expect(nextSignal({ ...v, along_m: 1200 })).toBeNull()
  })
})
