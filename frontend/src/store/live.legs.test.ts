import { beforeEach, describe, expect, it } from 'vitest'
import type { HospitalInfo, LegChangedMessage, TickVehicle } from '../api/types'
import { tick, vehicleDoc } from '../test/fixtures'
import { dwellElapsedS, nextJunction, useLiveStore, vehiclesMissingRoute } from './live'

const store = () => useLiveStore.getState()

const AMB = 'AMB-An2-1'
const TRANSPORT_ROUTE: [number, number][] = [
  [28.61, 77.21],
  [28.62, 77.21],
]
const HOSPITAL: HospitalInfo = {
  id: 'Hn9',
  name: 'City Hospital',
  location: [28.62, 77.21],
  emergency: 'yes',
}
const TRANSPORT_LEG = { seq: 1, kind: 'TRANSPORT', emergency: true } as const

function ambTick(along: number, extra: Partial<TickVehicle> = {}): TickVehicle {
  return {
    id: AMB,
    dispatch_id: 'd1',
    incident_id: 'i1',
    service: 'AMBULANCE',
    status: 'EN_ROUTE',
    pos: [28.611, 77.21],
    heading: 0,
    speed_mps: 10,
    eta_s: 100,
    along_m: along,
    distance_m: 1111.95,
    preempted_passed: 0,
    ...extra,
  }
}

const LEG_CHANGED: LegChangedMessage = {
  type: 'leg_changed',
  v: 1,
  city: 'delhi',
  vehicle_id: AMB,
  dispatch_id: 'd1',
  incident_id: 'i1',
  phase: 'TRANSPORTING',
  leg: TRANSPORT_LEG,
  destination: [28.62, 77.21],
  destination_info: {
    kind: 'HOSPITAL',
    station_id: 'Hn9',
    name: 'City Hospital',
    location: [28.62, 77.21],
  },
  predicted_eta_s: 120,
  route: TRANSPORT_ROUTE,
  corridor: [],
  hospital: HOSPITAL,
}

describe('live store: trip legs', () => {
  beforeEach(() => {
    store().reset('delhi')
    store().apply(
      {
        type: 'snapshot',
        v: 1,
        city: 'delhi',
        tick: 1,
        sim_ts: '2026-11-17T06:40:00+00:00',
        vehicles: [
          vehicleDoc({
            id: AMB,
            service: 'AMBULANCE',
            status: 'ON_SCENE',
            phase: 'ON_SCENE',
            leg: { seq: 0, kind: 'RESPONSE', emergency: true },
            along_m: 2088.13,
            dwell_s: 30,
            dwell_target_s: 480,
          }),
        ],
        signals: [],
      },
      0,
    )
  })

  it('tracks how long a stopped vehicle has dwelt, on the simulation clock', () => {
    const v = store().vehicles[AMB]!
    expect(dwellElapsedS(v, '2026-11-17T06:40:00+00:00')).toBe(30)
    expect(dwellElapsedS(v, '2026-11-17T06:41:00+00:00')).toBe(90)
    expect(v.dwell_target_s).toBe(480)
    // a later tick while still stopped keeps the start of the stop
    store().apply(
      {
        ...tick(2, [ambTick(2088.13, { status: 'ON_SCENE' })]),
        sim_ts: '2026-11-17T06:40:10+00:00',
      },
      500,
    )
    expect(dwellElapsedS(store().vehicles[AMB]!, '2026-11-17T06:40:10+00:00')).toBe(40)
  })

  it('leg_changed swaps in the new leg route and restarts the motion at its start', () => {
    store().apply(LEG_CHANGED, 100)
    const v = store().vehicles[AMB]!
    expect(v.phase).toBe('TRANSPORTING')
    expect(v.leg?.kind).toBe('TRANSPORT')
    expect(v.routeSeq).toBe(1)
    expect(v.route).toEqual(TRANSPORT_ROUTE)
    expect(v.hospital?.name).toBe('City Hospital')
    expect(v.destination_info?.kind).toBe('HOSPITAL')
    expect(v.motion.fromAlong).toBe(0)
    expect(v.motion.fromPos).toEqual([28.61, 77.21])
    expect(v.dwellSinceSim).toBeUndefined()
    expect(store().feed[0]?.text).toBe('AMB-An2-1 transport leg to City Hospital')
    expect(vehiclesMissingRoute(store())).toEqual([])
  })

  it('the re-sent vehicle_added of the same leg keeps the route object and the motion', () => {
    store().apply(LEG_CHANGED, 100)
    const before = store().vehicles[AMB]!
    store().apply(
      {
        type: 'vehicle_added',
        v: 1,
        city: 'delhi',
        vehicle: { ...ambTick(0), leg: TRANSPORT_LEG },
        route: [...TRANSPORT_ROUTE],
        corridor: [],
        destination: [28.62, 77.21],
      },
      120,
    )
    const after = store().vehicles[AMB]!
    expect(after.route).toBe(before.route) // same polyline: the map does not redraw it
    expect(after.motion).toBe(before.motion)
    expect(store().feed[0]?.kind).toBe('leg') // no second "rolling" entry
  })

  it('a tick on a newer leg than the drawn route drops the stale route and snaps', () => {
    store().apply(tick(2, [ambTick(15, { leg: TRANSPORT_LEG })]), 200)
    const v = store().vehicles[AMB]!
    expect(v.route).toBeUndefined()
    expect(v.motion.fromAlong).toBe(15)
    expect(vehiclesMissingRoute(store())).toEqual([AMB])
    // the REST back-fill of an older leg is ignored, the current leg's is taken
    store().mergeRoutes([
      vehicleDoc({ id: AMB, leg: { seq: 0, kind: 'RESPONSE', emergency: true } }),
    ])
    expect(store().vehicles[AMB]?.route).toBeUndefined()
    store().mergeRoutes([
      vehicleDoc({ id: AMB, leg: TRANSPORT_LEG, route: TRANSPORT_ROUTE, hospital: HOSPITAL }),
    ])
    expect(store().vehicles[AMB]?.routeSeq).toBe(1)
    expect(store().vehicles[AMB]?.hospital?.id).toBe('Hn9')
    expect(vehiclesMissingRoute(store())).toEqual([])
  })

  it('route_replaced splices the route in without restarting the motion', () => {
    store().apply(LEG_CHANGED, 100)
    store().apply(tick(2, [ambTick(500, { leg: TRANSPORT_LEG })]), 200)
    const motion = store().vehicles[AMB]!.motion
    const spliced: [number, number][] = [
      [28.61, 77.21],
      [28.615, 77.21],
      [28.615, 77.215],
      [28.62, 77.215],
    ]
    const replaced = (seq: number) =>
      store().apply(
        {
          type: 'route_replaced',
          v: 1,
          city: 'delhi',
          vehicle_id: AMB,
          dispatch_id: 'd1',
          incident_id: 'i1',
          leg: { ...TRANSPORT_LEG, seq },
          reroute_no: 1,
          reason: 'fire zone ahead',
          saved_s: 75,
          route: spliced,
          corridor: [],
        },
        250,
      )
    replaced(0) // an older leg's re-route is ignored
    expect(store().vehicles[AMB]?.route).toEqual(TRANSPORT_ROUTE)
    replaced(1)
    const v = store().vehicles[AMB]!
    expect(v.route).toEqual(spliced)
    expect(v.reroutes).toBe(1)
    expect(v.motion).toBe(motion)
    expect(store().feed[0]?.text).toBe('AMB-An2-1 re-routed, saves 75 s')
  })

  it('back in service removes the vehicle with a good feed entry', () => {
    store().apply(
      {
        type: 'vehicle_removed',
        v: 1,
        city: 'delhi',
        vehicle_id: AMB,
        dispatch_id: 'd1',
        incident_id: 'i1',
        reason: 'back_in_service',
      },
      300,
    )
    expect(store().vehicles).toEqual({})
    expect(store().feed[0]).toMatchObject({ text: 'AMB-An2-1 back in service', tone: 'good' })
  })

  it('records the hospital of a TRANSPORTING incident update', () => {
    store().apply(
      {
        type: 'incident_updated',
        v: 1,
        city: 'delhi',
        incident_id: 'i1',
        dispatch_id: 'd1',
        vehicle_id: AMB,
        status: 'TRANSPORTING',
        ts: '2026-11-17T06:48:00+00:00',
        hospital: HOSPITAL,
      },
      0,
    )
    expect(store().incidentStatus.i1).toMatchObject({ status: 'TRANSPORTING' })
    expect(store().incidentStatus.i1?.hospital?.name).toBe('City Hospital')
    expect(store().feed[0]?.text).toBe('Incident i1 → transporting')
  })
})

describe('nextJunction', () => {
  it('prefers the server next_movement and measures "be in lane by"', () => {
    const v = {
      ...vehicleDoc({ along_m: 400 }),
      next_movement: {
        signal_id: 'J10',
        pass_no: 0,
        movement: 'RIGHT' as const,
        lane_index: 2,
        lanes_total: 2,
        side: 'R' as const,
        text: 'Right lane (2 of 2): protected right turn',
        advise_at_m: 520,
        distance_m: 711.95,
        free_left: false,
      },
    }
    expect(nextJunction(v)).toMatchObject({
      name: 'ITO crossing',
      movement: 'RIGHT',
      lane_index: 2,
      lane_by_m: 120,
      distance_m: 711.95,
    })
    expect(nextJunction({ ...v, next_movement: null })).toBeNull()
  })

  it('falls back to the corridor for older servers', () => {
    expect(nextJunction(vehicleDoc({ along_m: 1000 }))).toMatchObject({
      signal_id: 'J10',
      side: 'L',
      movement: null,
      lane_index: null,
    })
  })
})
