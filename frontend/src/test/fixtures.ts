import { vi } from 'vitest'
import type {
  AnalyticsSummary,
  CityDetail,
  Incident,
  LiveVehicleDoc,
  TickMessage,
} from '../api/types'

export const DELHI: CityDetail = {
  id: 'delhi',
  name: 'Delhi',
  timezone: 'Asia/Kolkata',
  center: [28.6139, 77.209],
  bbox: [28.4, 76.84, 28.88, 77.35],
  zoom: 12,
  traffic: { hourly_congestion: Array.from({ length: 24 }, () => 1), avg_junction_delay_s: 35 },
  stations: [
    {
      id: 'Pw1',
      name: 'Police Station Connaught Place',
      service: 'POLICE',
      location: [28.63, 77.21],
      osm: 'way/1',
      total_units: 2,
      free_units: 1,
    },
    {
      id: 'An2',
      name: 'Far Hospital',
      service: 'AMBULANCE',
      location: [28.7, 77.3],
      osm: 'node/2',
      total_units: 2,
      free_units: 2,
    },
    {
      id: 'An3',
      name: 'Near Hospital (full)',
      service: 'AMBULANCE',
      location: [28.615, 77.21],
      osm: 'node/3',
      total_units: 2,
      free_units: 0,
    },
  ],
  signals: [{ id: 'J10', name: null, location: [28.62, 77.21], osm_node_ids: [10, 11] }],
}

export function incident(over: Partial<Incident> = {}): Incident {
  return {
    id: '31e6a6c9-83e3-4f3e-97bf-3954fe3bca02',
    city: 'delhi',
    type: 'CRIME',
    reported_type: 'POLICE',
    status: 'DISPATCHED',
    description: null,
    location: [28.6271, 77.2166],
    reported_at: '2026-11-17T06:30:00+00:00',
    dispatched_at: '2026-11-17T06:30:00+00:00',
    en_route_at: null,
    on_scene_at: null,
    resolved_at: null,
    cancelled_at: null,
    failed_at: null,
    failure_reason: null,
    ...over,
  }
}

export const ROUTE: [number, number][] = [
  [28.6, 77.2],
  [28.61, 77.2],
  [28.61, 77.21],
]

export function vehicleDoc(over: Partial<LiveVehicleDoc> = {}): LiveVehicleDoc {
  return {
    id: 'PCR-Pw1-1',
    dispatch_id: 'd1',
    incident_id: 'i1',
    service: 'POLICE',
    station_id: 'Pw1',
    incident_type: 'CRIME',
    status: 'EN_ROUTE',
    pos: [28.6, 77.2],
    heading: 0,
    speed_mps: 0,
    eta_s: 120,
    predicted_eta_s: 130,
    along_m: 0,
    distance_m: 2088.13,
    elapsed_s: 0,
    started_at: '2026-11-17T06:30:00+00:00',
    destination: [28.61, 77.21],
    claims: [],
    preempted_passed: 0,
    route: ROUTE,
    corridor: [
      {
        signal_id: 'J10',
        name: 'ITO crossing',
        pos: [28.61, 77.2],
        along_m: 1111.95,
        offset_m: 1,
        lane: 'L',
        approach_bearing: 0,
      },
    ],
    ...over,
  }
}

export function tick(
  n: number,
  vehicles: TickMessage['vehicles'],
  signals: TickMessage['signals'] = [],
): TickMessage {
  return {
    type: 'tick',
    v: 1,
    city: 'delhi',
    tick: n,
    sim_ts: '2026-11-17T06:30:00+00:00',
    ts: '2026-11-17T06:30:00+00:00',
    vehicles,
    signals,
  }
}

export const SUMMARY: AnalyticsSummary = {
  city: 'delhi',
  from: '2026-11-10T00:00:00+00:00',
  to: '2026-11-17T00:00:00+00:00',
  incidents: {
    total: 12,
    by_type: { MEDICAL: 7, FIRE: 2, CRIME: 3 },
    by_status: {
      OPEN: 0,
      DISPATCHED: 0,
      EN_ROUTE: 1,
      ON_SCENE: 0,
      RESOLVED: 10,
      CANCELLED: 1,
      FAILED: 0,
    },
  },
  response_time_s: { MEDICAL: { count: 6, p50_s: 312, p90_s: 540.2, mean_s: 330.1 } },
  dispatches: {
    total: 12,
    arrived: 10,
    avg_predicted_eta_s: 301.2,
    avg_actual_travel_s: 309.8,
    eta_mape: 0.031,
  },
  preemptions: {
    total: 41,
    conflicts: 2,
    distinct_signals: 30,
    top_signals: [{ signal_id: 'J2800811770', count: 4 }],
  },
  est_time_saved_s: {
    total: 1225,
    note: 'estimate: pre-empted junctions passed x city average junction delay',
  },
}

export interface MockRoute {
  method?: string
  match: RegExp
  status?: number
  body: unknown
  headers?: Record<string, string>
}

/** Replace global fetch with a router over regexes; returns the spy (calls are inspectable). */
export function mockFetch(routes: MockRoute[]) {
  const spy = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const method = init?.method ?? 'GET'
    const r = routes.find((x) => (x.method ?? 'GET') === method && x.match.test(url))
    if (!r)
      return Promise.resolve(
        new Response(JSON.stringify({ error: { code: 'not_found', message: url } }), {
          status: 404,
        }),
      )
    return Promise.resolve(
      new Response(JSON.stringify(r.body), {
        status: r.status ?? 200,
        headers: { 'content-type': 'application/json', ...(r.headers ?? {}) },
      }),
    )
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

/** URL string of a recorded fetch call argument. */
export function urlOf(input: RequestInfo | URL | undefined): string {
  if (input === undefined) return ''
  if (typeof input === 'string') return input
  return input instanceof URL ? input.href : input.url
}
