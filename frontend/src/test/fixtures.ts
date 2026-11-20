import { vi } from 'vitest'
import type {
  AnalyticsSummary,
  CityDetail,
  Incident,
  IncidentDetail,
  LatLng,
  LegRecord,
  LiveVehicleDoc,
  RouteCandidate,
  TickMessage,
  TimelineEvent,
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
    by_status: {
      DISPATCHED: 0,
      ON_SCENE: 0,
      TRANSPORTING: 0,
      AT_HOSPITAL: 0,
      RETURNING: 1,
      COMPLETED: 11,
      FAILED: 0,
    },
  },
  transport_time_s: { count: 5, p50_s: 480, p90_s: 700.1, mean_s: 501.2 },
  call_to_hospital_s: { count: 5, p50_s: 1460.3, p90_s: 1802, mean_s: 1495 },
  handover_to_back_in_service_s: { count: 5, p50_s: 1010, p90_s: 1350.4, mean_s: 1080.2 },
  hospitals: [
    { station_id: 'Hn123', name: 'AIIMS', count: 3 },
    { station_id: 'Hn9', name: 'City Hospital', count: 2 },
  ],
  legs: {
    RESPONSE: { count: 10, eta_mape: 0.021, eta_max_error: 0.064 },
    TRANSPORT: { count: 5, eta_mape: 0.018, eta_max_error: 0.041 },
    RETURN: { count: 9, eta_mape: 0.012, eta_max_error: 0.035 },
  },
  free_left_passed: 3,
  reroutes: 1,
  preemptions: {
    total: 41,
    conflicts: 2,
    distinct_signals: 30,
    top_signals: [{ signal_id: 'J2800811770', count: 4 }],
    outcomes: { CLEARED: 38, TIMEOUT: 1, CANCELLED: 2, REROUTED: 0 },
    green_extension: 9,
    red_truncation: 30,
  },
  est_time_saved_s: {
    total: 1225,
    note: 'estimate: pre-empted junctions passed x city average junction delay',
  },
}

const leg = (over: Partial<LegRecord> & Pick<LegRecord, 'seq' | 'kind'>): LegRecord => ({
  dispatch_id: 'd9',
  emergency: over.kind !== 'RETURN',
  origin: [28.6, 77.2],
  destination: { kind: 'SCENE', station_id: null, name: null, location: [28.61, 77.21] },
  route: ROUTE,
  distance_m: 2088.1,
  predicted_eta_s: 150,
  actual_travel_s: 152,
  eta_error_pct: 1.3,
  eta_model: 'kinematic-v3',
  planned_at: '2026-11-17T06:30:00+00:00',
  started_at: '2026-11-17T06:30:01+00:00',
  ended_at: '2026-11-17T06:32:33+00:00',
  end_reason: 'ARRIVED',
  reroutes: 0,
  started_trip_s: 0,
  ended_trip_s: 152,
  corridor: [],
  route_choice: null,
  hospital_choice: null,
  ...over,
})

/** A MEDICAL incident driven end to end: RESPONSE, TRANSPORT (to City Hospital), RETURN. */
export function medicalDetail(): IncidentDetail {
  return {
    ...incident({
      id: 'm1',
      type: 'MEDICAL',
      reported_type: 'MEDICAL',
      status: 'RESOLVED',
      on_scene_at: '2026-11-17T06:32:33+00:00',
      transport_started_at: '2026-11-17T06:41:00+00:00',
      at_hospital_at: '2026-11-17T06:45:30+00:00',
      resolved_at: '2026-11-17T06:55:30+00:00',
      hospital_station_id: 'Hn9',
    }),
    dispatches: [
      {
        id: 'd9',
        vehicle_id: 'AMB-An2-1',
        service: 'AMBULANCE',
        status: 'COMPLETED',
        station: { id: 'An2', name: 'Far Hospital', location: [28.6, 77.2] },
        route: ROUTE,
        distance_m: 2088.1,
        predicted_eta_s: 150,
        actual_travel_s: 152,
        est_time_saved_s: 70,
        eta_model: 'kinematic-v3',
        corridor: [],
        dispatched_at: '2026-11-17T06:30:00+00:00',
        arrived_at: '2026-11-17T06:32:33+00:00',
        ended_at: '2026-11-17T07:03:00+00:00',
        end_reason: 'CLEARED',
      },
    ],
    legs: [
      leg({
        seq: 0,
        kind: 'RESPONSE',
        route_choice: {
          chosen: 'A',
          reason: 'Route A: 0.4 min faster than B (B: 1 extra right turn)',
          tie_break: null,
          candidates: [
            routeCandidate('A', 150, ROUTE),
            routeCandidate('B', 174, [
              [28.6, 77.2],
              [28.6, 77.21],
              [28.61, 77.21],
            ]),
          ],
        },
      }),
      leg({
        seq: 1,
        kind: 'TRANSPORT',
        origin: [28.61, 77.21],
        destination: {
          kind: 'HOSPITAL',
          station_id: 'Hn9',
          name: 'City Hospital',
          location: [28.62, 77.21],
        },
        predicted_eta_s: 266.3,
        actual_travel_s: 270,
        eta_error_pct: 1.4,
        started_trip_s: 625,
        ended_trip_s: 895,
        corridor: [
          {
            signal_id: 'J20',
            name: 'Lodhi crossing',
            pos: [28.615, 77.21],
            along_m: 500,
            offset_m: 14,
            lane: 'L',
            approach_bearing: 0,
            movement: 'LEFT',
            free_left: true,
          },
        ],
        hospital_choice: {
          chosen: 'Hn9',
          reason: 'City Hospital: confirmed emergency department, 0.2 min more than Clinic',
          ed_tolerance_s: 335.8,
          candidates: [
            {
              station_id: 'Hn9',
              name: 'City Hospital',
              location: [28.62, 77.21],
              emergency: 'yes',
              healthcare: 'hospital',
              straight_line_m: 1100,
              cost_s: 279.3,
              eta_s: 266.3,
              distance_m: 2886.6,
              route: 'A',
              error: null,
            },
            {
              station_id: 'Hn8',
              name: 'Clinic',
              location: [28.6, 77.22],
              emergency: null,
              healthcare: null,
              straight_line_m: 900,
              cost_s: 268.7,
              eta_s: 259.7,
              distance_m: 1969.4,
              route: 'B',
              error: null,
            },
          ],
        },
      }),
      leg({
        seq: 2,
        kind: 'RETURN',
        emergency: false,
        origin: [28.62, 77.21],
        destination: {
          kind: 'STATION',
          station_id: 'An2',
          name: 'Far Hospital',
          location: [28.6, 77.2],
        },
        predicted_eta_s: 455,
        actual_travel_s: 455,
        eta_error_pct: 0,
        started_trip_s: 1495,
        ended_trip_s: 1950,
      }),
    ],
    hospital: {
      id: 'Hn9',
      name: 'City Hospital',
      location: [28.62, 77.21],
      emergency: 'yes',
      healthcare: 'hospital',
    },
    preemptions: [
      {
        event_id: 'clm:d9:J30',
        dispatch_id: 'd9',
        signal_id: 'J30',
        state: 'PREEMPTED',
        lane: 'R',
        approach_bearing: 270,
        eta_at_claim_s: 37.2,
        claimed_at: '2026-11-17T06:41:20+00:00',
        released_at: '2026-11-17T06:41:40+00:00',
        leg_seq: 1,
        movement: 'RIGHT',
        lane_index: 2,
        lanes_total: 2,
        free_left: false,
        required_lead_s: 38.6,
        transition_s: 5,
        queue_clear_s: 28.6,
        green_at: null,
        crossed_at: '2026-11-17T06:41:40+00:00',
        waited_s: 0,
        outcome: 'CLEARED',
        phase_state_at_claim: 'red_truncate',
      },
    ],
    timeline: [
      ev('reported', '06:30:00'),
      ev('dispatched', '06:30:00'),
      ev('arrived', '06:32:33', { travel_s: 152 }),
      ev('leg_planned', '06:32:40', { seq: 1, kind: 'TRANSPORT', predicted_eta_s: 266.3 }),
      ev('leg_started', '06:41:00', {
        seq: 1,
        kind: 'TRANSPORT',
        destination: { name: 'City Hospital' },
        predicted_eta_s: 266.3,
      }),
      ev('transport_started', '06:41:00', { hospital: { name: 'City Hospital' } }),
      ev('preemption_claimed', '06:41:20', {
        signal_id: 'J30',
        lane: 'R',
        leg_seq: 1,
        movement: 'RIGHT',
      }),
      ev('arrived_hospital', '06:45:30', { travel_s: 270 }),
      ev('handover_complete', '06:55:30', { handover_s: 600 }),
      ev('leg_started', '06:55:31', {
        seq: 2,
        kind: 'RETURN',
        destination: { name: 'Far Hospital' },
        predicted_eta_s: 455,
      }),
      ev('back_in_service', '07:03:00'),
    ],
  }
}

function routeCandidate(label: string, cost: number, route: LatLng[]): RouteCandidate {
  return {
    label,
    source: 'osrm',
    distance_m: 2088,
    base_time_s: cost - 30,
    congested_time_s: cost,
    junction_delay_s: 0,
    penalties_s: 0,
    cost_s: cost,
    crowd_index: 0.38,
    signals: 2,
    right_turns: label === 'B' ? 1 : 0,
    contention_junctions: [],
    incident_zones: [],
    congested_road: 'Mathura Road',
    route,
  }
}

function ev(type: string, hhmmss: string, payload: Record<string, unknown> = {}): TimelineEvent {
  return {
    event_id: `${type}:${hhmmss}`,
    dispatch_id: 'd9',
    type,
    ts: `2026-11-17T${hhmmss}+00:00`,
    payload,
  }
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
