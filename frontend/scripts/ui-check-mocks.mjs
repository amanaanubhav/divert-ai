// Deterministic API + WebSocket mocks for scripts/clickability.mjs (mock mode).
// Shapes follow src/api/types.ts (docs/api.md). Only what the UI renders is filled in.
// The live view carries every trip-leg state the UI draws: a TRANSPORT leg with a patient and a
// destination hospital, a RESPONSE leg with a speed advisory, a unit on scene, a unit at the
// hospital waiting for its RETURN plan ("planning route"), a RETURN leg, the five junction
// states and a free-left junction. History details have legs, a hospital choice and route
// choices with alternative geometries; analytics have the trip-leg metrics.

const CITIES = [
  {
    id: 'delhi',
    name: 'Delhi',
    timezone: 'Asia/Kolkata',
    center: [28.6139, 77.209],
    bbox: [28.4, 76.84, 28.88, 77.35],
    zoom: 12,
  },
  {
    id: 'kolkata',
    name: 'Kolkata',
    timezone: 'Asia/Kolkata',
    center: [22.5726, 88.3639],
    bbox: [22.45, 88.25, 22.65, 88.5],
    zoom: 13,
  },
  {
    id: 'bengaluru',
    name: 'Bengaluru',
    timezone: 'Asia/Kolkata',
    center: [12.9716, 77.5946],
    bbox: [12.83, 77.46, 13.14, 77.78],
    zoom: 12,
  },
]

export const CITY_IDS = CITIES.map((c) => c.id)

const SERVICES = ['AMBULANCE', 'FIRE_ENGINE', 'POLICE']
const TYPES = ['MEDICAL', 'FIRE', 'CRIME']
const SERVICE_FOR_TYPE = { MEDICAL: 'AMBULANCE', FIRE: 'FIRE_ENGINE', CRIME: 'POLICE' }
const PREFIX = { AMBULANCE: 'AMB', FIRE_ENGINE: 'FIRE', POLICE: 'PCR' }
const RANK = { AMBULANCE: 1, FIRE_ENGINE: 2, POLICE: 3 }
const STATUSES = ['RESOLVED', 'RESOLVED', 'RESOLVED', 'CANCELLED', 'FAILED', 'RESOLVED']
/** The first incidents are active; their statuses drive the live view (see buildCity). */
const ACTIVE = ['TRANSPORTING', 'EN_ROUTE', 'ON_SCENE', 'AT_HOSPITAL']

/** Small deterministic PRNG (mulberry32) so every run draws the same city. */
function rng(seed) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function seedOf(s) {
  let h = 2166136261
  for (const ch of s) h = Math.imul(h ^ ch.charCodeAt(0), 16777619)
  return h >>> 0
}

const uuid = (r) =>
  'xxxxxxxx-xxxx-4xxx-8xxx-xxxxxxxxxxxx'.replace(/x/g, () => Math.floor(r() * 16).toString(16))

function inner(city, r, spread = 0.6) {
  const [s, w, n, e] = city.bbox
  const lat = city.center[0] + (r() - 0.5) * (n - s) * spread
  const lng = city.center[1] + (r() - 0.5) * (e - w) * spread
  return [Number(lat.toFixed(5)), Number(lng.toFixed(5))]
}

function metres(a, b) {
  const R = 6371008.8
  const toRad = (d) => (d * Math.PI) / 180
  const dLat = toRad(b[0] - a[0])
  const dLng = toRad(b[1] - a[1])
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLng / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(h))
}

/** An L-shaped "street" route with a few vertices; `alt` takes the other corner. */
function routeBetween(from, to, alt = false) {
  const mid = alt ? [to[0], from[1]] : [from[0], to[1]]
  const pts = []
  for (let i = 0; i <= 3; i++)
    pts.push([from[0] + ((mid[0] - from[0]) * i) / 3, from[1] + ((mid[1] - from[1]) * i) / 3])
  for (let i = 1; i <= 3; i++)
    pts.push([mid[0] + ((to[0] - mid[0]) * i) / 3, mid[1] + ((to[1] - mid[1]) * i) / 3])
  let total = 0
  const cum = [0]
  for (let i = 1; i < pts.length; i++) {
    total += metres(pts[i - 1], pts[i])
    cum.push(total)
  }
  return { route: pts, cum, distance: total }
}

function posAlong(route, cum, along) {
  for (let i = 1; i < route.length; i++) {
    if (cum[i] >= along) {
      const seg = cum[i] - cum[i - 1] || 1
      const t = (along - cum[i - 1]) / seg
      return [
        route[i - 1][0] + (route[i][0] - route[i - 1][0]) * t,
        route[i - 1][1] + (route[i][1] - route[i - 1][1]) * t,
      ]
    }
  }
  return route[route.length - 1]
}

const MOVES = ['STRAIGHT', 'RIGHT', 'LEFT', 'STRAIGHT']
const ADVICE_TEXT = {
  STRAIGHT: (i, n) => `Right lane (${i} of ${n}): straight on, traffic yields left`,
  RIGHT: (i, n) => `Right lane (${i} of ${n}): protected right turn`,
  LEFT: (i, n) => `Left lane (${i} of ${n}): left turn`,
}

/** Four corridor junctions with movement + lane advice; the left turn is a free left if asked. */
function corridorFor(route, cum, signals, offset = 0, { freeLeft = false } = {}) {
  return [1, 2, 4, 5].map((i, k) => {
    const movement = MOVES[k]
    const lanes = 2 + (k % 2)
    const laneIndex = movement === 'LEFT' ? 1 : lanes
    const along = Number(cum[i].toFixed(2))
    const free = freeLeft && movement === 'LEFT'
    return {
      signal_id: signals[offset + k]?.id ?? `J${offset + i}`,
      name: k % 2 === 0 ? `${['Ring Road', 'MG Road', 'Park Street', 'ITO'][k]} crossing` : null,
      pos: route[i],
      along_m: along,
      offset_m: free ? 14 : 2,
      lane: movement === 'LEFT' ? 'L' : 'R',
      approach_bearing: k * 90,
      pass_no: 0,
      movement,
      turn_deg: movement === 'LEFT' ? -88 : movement === 'RIGHT' ? 90 : 0,
      exit_bearing: null,
      roundabout_exit: null,
      free_left: free,
      legs: 4,
      junction_bearings: [0, 90, 180, 270],
      advice: {
        movement,
        lane_index: laneIndex,
        lanes_total: lanes,
        side: movement === 'LEFT' ? 'L' : 'R',
        advise_at_m: Math.max(0, Number((along - 150).toFixed(1))),
        free_left: free,
        road_class: 'ARTERIAL',
        lookahead: false,
        text: ADVICE_TEXT[movement](laneIndex, lanes),
      },
    }
  })
}

/** The next junction ahead of a driving vehicle (as the worker's next_movement). */
function nextMovementOf(v) {
  if (v.status !== 'EN_ROUTE') return null
  const c = v.corridor.find((x) => x.along_m > v.along_m)
  if (!c) return null
  return {
    signal_id: c.signal_id,
    pass_no: 0,
    movement: c.movement,
    lane_index: c.advice.lane_index,
    lanes_total: c.advice.lanes_total,
    side: c.lane,
    text: c.advice.text,
    advise_at_m: c.advice.advise_at_m,
    distance_m: Number((c.along_m - v.along_m).toFixed(1)),
    free_left: c.free_left,
  }
}

function candidate(label, distance, timeS, route, extra = {}) {
  return {
    label,
    source: 'osrm',
    distance_m: Number(distance.toFixed(1)),
    base_time_s: Number((timeS * 0.75).toFixed(1)),
    congested_time_s: Number(timeS.toFixed(1)),
    junction_delay_s: label === 'A' ? 6 : 12,
    penalties_s: 0,
    cost_s: Number((timeS + (label === 'A' ? 6 : 12)).toFixed(1)),
    crowd_index: label === 'A' ? 0.31 : 0.52,
    signals: 4,
    right_turns: label === 'A' ? 1 : 2,
    contention_junctions: [],
    incident_zones: [],
    congested_road: label === 'A' ? 'Janpath' : 'Ring Road',
    route,
    ...extra,
  }
}

/** Route A (the drawn route) and an alternative B on the other corner, with geometries. */
function routeChoiceFor(from, to, stationId = null) {
  const a = routeBetween(from, to)
  const b = routeBetween(from, to, true)
  const ta = a.distance / 11
  const tb = b.distance / 9
  return {
    ...(stationId ? { station_id: stationId } : {}),
    chosen: 'A',
    reason: `Route A: ${((tb - ta) / 60).toFixed(1)} min faster than B (B: Ring Road congested, 1 extra right turn)`,
    tie_break: null,
    candidates: [candidate('A', a.distance, ta, a.route), candidate('B', b.distance, tb, b.route)],
  }
}

const iso = (ms) => new Date(ms).toISOString()

const hospitalInfo = (s) => ({ id: s.id, name: s.name, location: s.location, emergency: 'yes' })

/** Everything the UI fetches for one city, generated once per city id. */
function buildCity(id) {
  const base = CITIES.find((c) => c.id === id) ?? CITIES[0]
  const city = { ...base, id }
  const r = rng(seedOf(id))
  const now = Date.now()

  const stations = []
  for (let i = 0; i < 18; i++) {
    const service = SERVICES[i % 3]
    const total = 2 + (i % 3)
    stations.push({
      id: `${service[0]}${i}`,
      name:
        i % 5 === 4
          ? null
          : `${['Central', 'North', 'South', 'East', 'West', 'Old Town'][i % 6]} ${
              service === 'AMBULANCE'
                ? 'Hospital'
                : service === 'FIRE_ENGINE'
                  ? 'Fire Station'
                  : 'Police Station'
            }`,
      service,
      location: inner(city, r, 0.8),
      osm: `node/${1000 + i}`,
      total_units: total,
      free_units: i % 4 === 0 ? 0 : total - 1,
    })
  }
  const signals = []
  for (let i = 0; i < 60; i++)
    signals.push({
      id: `J${100 + i}`,
      name: i % 3 ? null : `Junction ${i}`,
      location: inner(city, r, 0.9),
      osm_node_ids: [i],
    })

  const detail = {
    ...city,
    traffic: {
      hourly_congestion: Array.from({ length: 24 }, (_, h) => 0.8 + (h % 6) / 10),
      avg_junction_delay_s: 35,
    },
    stations,
    signals,
  }
  const home = (service, n = 0) => stations.filter((s) => s.service === service)[n] ?? stations[0]
  const hospital = stations.filter((s) => s.service === 'AMBULANCE' && s.name)[1] ?? stations[0]

  // History: 60 incidents, newest first; the first 4 are active (they also drive the live view).
  const incidents = []
  for (let i = 0; i < 60; i++) {
    const type = TYPES[i % 3]
    const reported = now - i * 37 * 60_000 - 90_000
    const active = i < ACTIVE.length
    const status = active ? ACTIVE[i] : STATUSES[i % STATUSES.length]
    const reached = ['RESOLVED', 'ON_SCENE', 'TRANSPORTING', 'AT_HOSPITAL'].includes(status)
    const onScene = reached ? reported + (240 + (i % 7) * 31) * 1000 : null
    const transported = type === 'MEDICAL' && onScene && status !== 'ON_SCENE'
    incidents.push({
      id: uuid(r),
      city: id,
      type,
      reported_type: type,
      status,
      description:
        i % 4 === 0
          ? [
              '2 injured, left lane blocked',
              'Smoke from 3rd floor',
              'Shop break-in reported',
              'Elderly person collapsed',
            ][(i / 4) % 4]
          : null,
      location: inner(city, r, 0.5),
      reported_at: iso(reported),
      dispatched_at: iso(reported + 2000),
      en_route_at: iso(reported + 4000),
      on_scene_at: onScene ? iso(onScene) : null,
      resolved_at: status === 'RESOLVED' && onScene ? iso(onScene + 1_500_000) : null,
      cancelled_at: status === 'CANCELLED' ? iso(reported + 120_000) : null,
      failed_at: status === 'FAILED' ? iso(reported + 5000) : null,
      failure_reason: status === 'FAILED' ? 'no_unit_available' : null,
      hospital_station_id: transported ? hospital.id : null,
      transport_started_at: transported ? iso(onScene + 480_000) : null,
      at_hospital_at:
        transported && status !== 'TRANSPORTING' ? iso(onScene + 480_000 + 300_000) : null,
    })
  }

  // Live vehicles: one per active incident plus a fire engine driving back (RETURN leg).
  const vehicle = (inc, k, spec) => {
    const service = SERVICE_FOR_TYPE[inc.type]
    const station = home(service, spec.unit ?? 0)
    const { route, cum, distance } = routeBetween(spec.from, spec.to)
    const along = spec.along === 'end' ? distance : distance * spec.along
    const v = {
      id: `${PREFIX[service]}-${station.id}-${(spec.unit ?? 0) + 1}`,
      dispatch_id: `d-${inc.id.slice(0, 8)}`,
      incident_id: inc.id,
      service,
      station_id: station.id,
      incident_type: inc.type,
      status: spec.along === 'end' ? 'ON_SCENE' : 'EN_ROUTE',
      phase: spec.phase,
      leg: spec.leg,
      leg_kind: spec.leg.kind,
      emergency: spec.leg.emergency,
      rank: spec.leg.kind === 'TRANSPORT' ? 0 : RANK[service],
      pos: posAlong(route, cum, along),
      heading: 90,
      speed_mps: spec.along === 'end' ? 0 : 11.5,
      eta_s: spec.along === 'end' ? 0 : Math.round((distance - along) / 11.5),
      predicted_eta_s: Math.round(distance / 11),
      along_m: along,
      distance_m: distance,
      elapsed_s: 60,
      started_at: inc.dispatched_at,
      destination: spec.to,
      destination_info: spec.dest,
      claims: [],
      preempted_passed: k + 1,
      planning: spec.planning ?? false,
      plan_error: null,
      speed_advisory_mps: spec.advisory ?? null,
      incident_location: inc.location,
      home: station.location,
      hospital: spec.hospital ?? null,
      dwell_s: spec.dwell ?? 0,
      dwell_target_s: spec.dwellTarget ?? 0,
      trip_s: 900,
      reroutes: 0,
      route,
      corridor: corridorFor(route, cum, signals, 4 * (k + 1), { freeLeft: k === 0 }),
      _cum: cum,
    }
    v.next_movement = nextMovementOf(v)
    return v
  }
  const scene = (service, n = 0) => ({
    kind: 'STATION',
    station_id: home(service, n).id,
    name: home(service, n).name,
    location: home(service, n).location,
  })
  const toHospital = {
    kind: 'HOSPITAL',
    station_id: hospital.id,
    name: hospital.name,
    location: hospital.location,
  }
  const [transporting, enRoute, onScene, atHospital] = incidents
  // a fire engine driving back from a resolved fire (RETURN leg, seq 1)
  const returning = incidents.find(
    (x, i) => i >= ACTIVE.length && x.type === 'FIRE' && x.status === 'RESOLVED',
  )
  const sceneDest = (inc) => ({
    kind: 'SCENE',
    station_id: null,
    name: null,
    location: inc.location,
  })
  const vehicles = [
    vehicle(transporting, 0, {
      from: transporting.location,
      to: hospital.location,
      along: 0.3,
      phase: 'TRANSPORTING',
      leg: { seq: 1, kind: 'TRANSPORT', emergency: true },
      dest: toHospital,
      hospital: hospitalInfo(hospital),
    }),
    vehicle(enRoute, 1, {
      from: home('FIRE_ENGINE').location,
      to: enRoute.location,
      along: 0.45,
      phase: 'DISPATCHED',
      leg: { seq: 0, kind: 'RESPONSE', emergency: true },
      dest: sceneDest(enRoute),
      advisory: 6.5,
    }),
    vehicle(onScene, 2, {
      from: home('POLICE').location,
      to: onScene.location,
      along: 'end',
      phase: 'ON_SCENE',
      leg: { seq: 0, kind: 'RESPONSE', emergency: true },
      dest: sceneDest(onScene),
      dwell: 200,
      dwellTarget: 600,
    }),
    vehicle(atHospital, 3, {
      unit: 1,
      from: atHospital.location,
      to: hospital.location,
      along: 'end',
      phase: 'AT_HOSPITAL',
      leg: { seq: 1, kind: 'TRANSPORT', emergency: true },
      dest: toHospital,
      hospital: hospitalInfo(hospital),
      planning: true,
      dwell: 610,
      dwellTarget: 600,
    }),
    vehicle(returning, 4, {
      unit: 1,
      from: returning.location,
      to: home('FIRE_ENGINE', 1).location,
      along: 0.2,
      phase: 'RETURNING',
      leg: { seq: 1, kind: 'RETURN', emergency: false },
      dest: scene('FIRE_ENGINE', 1),
    }),
  ]

  // The five junction states: queued (2 claimants), pre-empted, transitioning, recovering.
  const [amb, fire] = vehicles
  const junction = (v, c, state, claimants, phaseInfo) => ({
    signal_id: c.signal_id,
    state,
    pos: c.pos,
    vehicle_id: v.id,
    service: v.service,
    lane: c.lane,
    approach_bearing: c.approach_bearing,
    eta_s: state === 'RECOVERING' ? 0 : 18,
    phase_info: {
      served_phase: state === 'RECOVERING' ? null : 1,
      phases: 4,
      cycle_s: 120,
      geometry: 'osrm',
      transition_s_left: state === 'TRANSITIONING' ? 6 : 0,
      expected_green_at: null,
      hold_s: state === 'PREEMPTED' || state === 'QUEUED' ? 8.5 : 0,
      state_at_claim: state === 'RECOVERING' ? null : 'red_truncate',
      ...phaseInfo,
    },
    claimants,
  })
  const claimant = (v, c, phaseState, queuePos, eta) => ({
    vehicle_id: v.id,
    service: v.service,
    eta_s: eta,
    state: phaseState === 'QUEUED' ? 'CONFLICT' : 'PREEMPTED',
    phase_state: phaseState,
    movement: c.movement,
    rank: v.rank,
    lane: c.lane,
    lane_index: c.advice.lane_index,
    lanes_total: c.advice.lanes_total,
    phase: queuePos,
    queue_pos: queuePos,
    expected_green_at: phaseState === 'QUEUED' ? iso(now + 21_000) : null,
  })
  const ambJunctions = amb.corridor.filter((c) => !c.free_left)
  const ambPre = ambJunctions.find((c) => c.along_m > amb.along_m) ?? ambJunctions[0]
  const ambTrans = ambJunctions.filter((c) => c !== ambPre).at(-1)
  const fireNext = fire.corridor.find((c) => c.along_m > fire.along_m) ?? fire.corridor.at(-1)
  const recovering =
    fire.corridor.find((c) => c.along_m <= fire.along_m && c !== fireNext) ??
    vehicles[4].corridor[0]
  const activeSignals = [
    // the ambulance (patient on board, rank 0) is served; the fire engine waits
    junction(amb, fireNext, 'QUEUED', [
      claimant(amb, fireNext, 'PREEMPTED', 0, 9),
      claimant(fire, fireNext, 'QUEUED', 1, 19),
    ]),
    junction(amb, ambPre, 'PREEMPTED', [claimant(amb, ambPre, 'PREEMPTED', 0, 18)]),
    junction(amb, ambTrans, 'TRANSITIONING', [claimant(amb, ambTrans, 'TRANSITIONING', 0, 34)]),
    junction(fire, recovering, 'RECOVERING', [], {
      recovery_phase: 2,
      recovering_until: iso(now + 14_000),
    }),
  ]

  const analytics = {
    city: id,
    from: iso(now - 7 * 86_400_000),
    to: iso(now),
    incidents: {
      total: 57,
      by_type: { MEDICAL: 25, FIRE: 12, CRIME: 20 },
      by_status: {
        OPEN: 0,
        DISPATCHED: 0,
        EN_ROUTE: 1,
        ON_SCENE: 1,
        TRANSPORTING: 1,
        AT_HOSPITAL: 1,
        RESOLVED: 43,
        CANCELLED: 6,
        FAILED: 4,
      },
    },
    response_time_s: {
      MEDICAL: { count: 22, p50_s: 312, p90_s: 540.2, mean_s: 330.1 },
      FIRE: { count: 10, p50_s: 405, p90_s: 610, mean_s: 420 },
      CRIME: { count: 12, p50_s: 288, p90_s: 470, mean_s: 300 },
    },
    dispatches: {
      total: 53,
      arrived: 47,
      avg_predicted_eta_s: 301.2,
      avg_actual_travel_s: 309.8,
      eta_mape: 0.031,
      by_status: {
        DISPATCHED: 1,
        ON_SCENE: 1,
        TRANSPORTING: 1,
        AT_HOSPITAL: 1,
        RETURNING: 2,
        COMPLETED: 43,
        FAILED: 4,
      },
    },
    transport_time_s: { count: 19, p50_s: 480, p90_s: 700.1, mean_s: 501.2 },
    call_to_hospital_s: { count: 19, p50_s: 1460.3, p90_s: 1802, mean_s: 1495 },
    handover_to_back_in_service_s: { count: 18, p50_s: 1010, p90_s: 1350.4, mean_s: 1080.2 },
    hospitals: stations
      .filter((s) => s.service === 'AMBULANCE')
      .slice(0, 5)
      .map((s, i) => ({ station_id: s.id, name: s.name, count: 7 - i })),
    legs: {
      RESPONSE: { count: 47, eta_mape: 0.021, eta_max_error: 0.064 },
      TRANSPORT: { count: 19, eta_mape: 0.018, eta_max_error: 0.041 },
      RETURN: { count: 44, eta_mape: 0.012, eta_max_error: 0.035 },
    },
    free_left_passed: 11,
    reroutes: 2,
    preemptions: {
      total: 141,
      conflicts: 6,
      distinct_signals: 64,
      top_signals: signals.slice(0, 5).map((s, i) => ({ signal_id: s.id, count: 9 - i })),
      outcomes: { CLEARED: 131, TIMEOUT: 2, CANCELLED: 6, REROUTED: 2 },
      green_extension: 37,
      red_truncation: 104,
    },
    est_time_saved_s: {
      total: 4935,
      note: 'estimate: pre-empted junctions passed x city average junction delay',
    },
  }

  return { city, detail, incidents, vehicles, activeSignals, analytics, hospital }
}

const cache = new Map()
const cityData = (id) => {
  if (!cache.has(id)) cache.set(id, buildCity(id))
  return cache.get(id)
}

const publicVehicle = ({ _cum, ...v }) => v

const DISPATCH_STATUS = {
  DISPATCHED: 'DISPATCHED',
  EN_ROUTE: 'DISPATCHED',
  ON_SCENE: 'ON_SCENE',
  TRANSPORTING: 'TRANSPORTING',
  AT_HOSPITAL: 'AT_HOSPITAL',
  RESOLVED: 'COMPLETED',
  CANCELLED: 'COMPLETED',
  FAILED: 'FAILED',
}

/** One leg record of the incident detail. */
function legRecord(dispatchId, seq, kind, from, to, dest, t0, opts = {}) {
  const { route, cum, distance } = routeBetween(from, to)
  const predicted = Math.round(distance / 11)
  const actual = opts.driving ? null : Math.round(predicted * (1 + (seq % 2 ? 0.014 : -0.006)))
  return {
    dispatch_id: dispatchId,
    seq,
    kind,
    emergency: kind !== 'RETURN',
    origin: from,
    destination: dest,
    route,
    distance_m: Number(distance.toFixed(1)),
    predicted_eta_s: predicted,
    actual_travel_s: actual,
    eta_error_pct: actual ? Number(((100 * (actual - predicted)) / actual).toFixed(1)) : null,
    eta_model: 'kinematic-v3',
    planned_at: iso(t0 - 30_000),
    started_at: iso(t0),
    ended_at: actual ? iso(t0 + actual * 1000) : null,
    end_reason: actual ? 'ARRIVED' : null,
    reroutes: 0,
    started_trip_s: opts.tripS ?? 0,
    ended_trip_s: actual ? (opts.tripS ?? 0) + actual : null,
    corridor: corridorFor(route, cum, opts.signals ?? [], 4 * seq, {
      freeLeft: kind === 'TRANSPORT',
    }),
    route_choice: routeChoiceFor(from, to, opts.stationId),
    hospital_choice: opts.hospitalChoice ?? null,
    _cum: cum,
  }
}

function incidentDetail(inc, data) {
  const service = SERVICE_FOR_TYPE[inc.type]
  const station = data.detail.stations.find((s) => s.service === service) ?? data.detail.stations[0]
  const dispatchId = `d-${inc.id.slice(0, 8)}`
  const signals = data.detail.signals
  const t0 = Date.parse(inc.dispatched_at)
  const onSceneMs = inc.on_scene_at ? Date.parse(inc.on_scene_at) : null
  const dest = (s, kind) => ({ kind, station_id: s.id, name: s.name, location: s.location })
  const sceneDest = { kind: 'SCENE', station_id: null, name: null, location: inc.location }
  const h = data.hospital

  const legs = [
    legRecord(dispatchId, 0, 'RESPONSE', station.location, inc.location, sceneDest, t0, {
      signals,
      stationId: station.id,
      driving: !onSceneMs,
    }),
  ]
  const transported = Boolean(inc.transport_started_at)
  if (transported) {
    const hospitalChoice = {
      chosen: h.id,
      reason: `${h.name}: confirmed emergency department, 0.4 min more than the nearest hospital (within the 3.0 min tolerance)`,
      ed_tolerance_s: 180,
      candidates: data.detail.stations
        .filter((s) => s.service === 'AMBULANCE')
        .slice(0, 6)
        .map((s, i) => ({
          station_id: s.id,
          name: s.name,
          location: s.location,
          emergency: s.id === h.id ? 'yes' : i % 2 ? null : 'no',
          healthcare: 'hospital',
          straight_line_m: 900 + i * 350,
          cost_s: s.id === h.id ? 290 : 266 + i * 31,
          eta_s: s.id === h.id ? 280 : 256 + i * 31,
          distance_m: 2100 + i * 420,
          route: 'A',
          error: null,
        })),
    }
    legs.push(
      legRecord(
        dispatchId,
        1,
        'TRANSPORT',
        inc.location,
        h.location,
        dest(h, 'HOSPITAL'),
        Date.parse(inc.transport_started_at),
        {
          signals,
          driving: inc.status === 'TRANSPORTING',
          tripS: 625,
          hospitalChoice,
          stationId: h.id,
        },
      ),
    )
  }
  if (inc.status === 'RESOLVED') {
    const from = transported ? h.location : inc.location
    const start = Date.parse(inc.resolved_at)
    legs.push(
      legRecord(
        dispatchId,
        legs.length,
        'RETURN',
        from,
        station.location,
        dest(station, 'STATION'),
        start,
        {
          signals,
          tripS: 1495,
        },
      ),
    )
  }

  const preemptions = legs.flatMap((l) =>
    l.kind === 'RETURN'
      ? []
      : l.corridor
          .filter((c) => !c.free_left)
          .slice(0, 2)
          .map((c, i) => ({
            event_id: `p${l.seq}${i}`,
            dispatch_id: dispatchId,
            signal_id: c.signal_id,
            state: i === 1 && l.seq === 0 ? 'CONFLICT' : 'PREEMPTED',
            lane: c.lane,
            approach_bearing: c.approach_bearing,
            eta_at_claim_s: 20 + i * 3,
            claimed_at: iso(Date.parse(l.started_at) + (i + 1) * 30_000),
            released_at: iso(Date.parse(l.started_at) + (i + 1) * 30_000 + 25_000),
            leg_seq: l.seq,
            movement: c.movement,
            lane_index: c.advice.lane_index,
            lanes_total: c.advice.lanes_total,
            free_left: false,
            required_lead_s: 24 + i,
            transition_s: i === 0 ? 0 : 5,
            queue_clear_s: 12 + i,
            green_at: null,
            crossed_at: iso(Date.parse(l.started_at) + (i + 1) * 30_000 + 25_000),
            waited_s: i === 1 && l.seq === 0 ? 4 : 0,
            outcome: 'CLEARED',
            phase_state_at_claim: i === 0 ? 'green_extend' : 'red_truncate',
          })),
  )

  const legEvents = legs.flatMap((l) => {
    const out = []
    if (l.seq > 0) {
      out.push([
        'leg_planned',
        iso(Date.parse(l.started_at) - 30_000),
        {
          seq: l.seq,
          kind: l.kind,
          predicted_eta_s: l.predicted_eta_s,
          destination: l.destination,
        },
      ])
      out.push([
        'leg_started',
        l.started_at,
        {
          seq: l.seq,
          kind: l.kind,
          predicted_eta_s: l.predicted_eta_s,
          destination: l.destination,
        },
      ])
    }
    if (l.kind === 'TRANSPORT')
      out.push(['transport_started', l.started_at, { hospital: hospitalInfo(h) }])
    if (l.ended_at)
      out.push([
        'leg_ended',
        l.ended_at,
        { seq: l.seq, kind: l.kind, end_reason: 'ARRIVED', travel_s: l.actual_travel_s },
      ])
    if (l.kind === 'TRANSPORT' && l.ended_at) {
      out.push(['arrived_hospital', l.ended_at, { travel_s: l.actual_travel_s }])
      if (inc.resolved_at) out.push(['handover_complete', inc.resolved_at, { handover_s: 600 }])
    }
    if (l.kind === 'RETURN' && l.ended_at) out.push(['back_in_service', l.ended_at, {}])
    return out
  })
  const timeline = [
    ['reported', inc.reported_at],
    ['dispatched', inc.dispatched_at],
    ['en_route', inc.en_route_at],
    ...preemptions.map((p) => [
      p.state === 'CONFLICT' ? 'preemption_conflict' : 'preemption_claimed',
      p.claimed_at,
      { signal_id: p.signal_id, lane: p.lane, leg_seq: p.leg_seq, movement: p.movement },
    ]),
    [
      'arrived',
      inc.on_scene_at,
      legs[0].actual_travel_s ? { travel_s: legs[0].actual_travel_s } : null,
    ],
    ...legEvents,
    ['cleared', transported ? null : inc.resolved_at],
    ['cancelled', inc.cancelled_at],
    ['failed', inc.failed_at, inc.failure_reason ? { reason: inc.failure_reason } : null],
  ]
    .filter(([, ts]) => ts)
    .sort((a, b) => Date.parse(a[1]) - Date.parse(b[1]))
    .map(([type, ts, payload], i) => ({
      event_id: `e${i}`,
      dispatch_id: dispatchId,
      type,
      ts,
      payload: payload ?? null,
    }))

  // samples along every leg (the dwell between legs shows as a gap)
  const points = legs.flatMap((l) =>
    l.route.map((pos, i) => ({
      dispatch_id: dispatchId,
      ts: iso(Date.parse(l.started_at) + i * 20_000),
      pos,
      speed_mps: 11,
      along_m: l._cum[i],
      leg_seq: l.seq,
    })),
  )
  const response = legs[0]
  const publicLeg = ({ _cum, ...l }) => l
  return {
    detail: {
      ...inc,
      dispatches: [
        {
          id: dispatchId,
          vehicle_id: `${PREFIX[service]}-${station.id}-1`,
          service,
          status: DISPATCH_STATUS[inc.status] ?? 'DISPATCHED',
          station: { id: station.id, name: station.name, location: station.location },
          route: response.route,
          distance_m: response.distance_m,
          predicted_eta_s: response.predicted_eta_s,
          actual_travel_s: response.actual_travel_s,
          est_time_saved_s: 70,
          eta_model: 'kinematic-v3',
          corridor: response.corridor,
          route_choice: response.route_choice,
          dispatched_at: inc.dispatched_at,
          arrived_at: inc.on_scene_at,
          ended_at: inc.resolved_at ?? inc.cancelled_at ?? inc.failed_at,
          end_reason:
            inc.status === 'RESOLVED' ? 'CLEARED' : inc.status === 'CANCELLED' ? 'CANCELLED' : null,
        },
      ],
      legs: legs.map(publicLeg),
      hospital: transported ? { ...hospitalInfo(h), healthcare: 'hospital' } : null,
      preemptions,
      timeline,
    },
    track: { incident_id: inc.id, points },
  }
}

const json = (body, status = 200) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

/**
 * Answers one API request. `mutationsOnly` (live mode): only POST/DELETE are answered here so
 * the check never creates or cancels real incidents; everything else returns null (pass through).
 */
export function answer(method, url, postBody, { mutationsOnly = false } = {}) {
  const u = new URL(url)
  const p = u.pathname
  let m
  if (method === 'POST' && (m = p.match(/^\/api\/v1\/cities\/([^/]+)\/incidents$/))) {
    const id = m[1]
    const body = postBody ? JSON.parse(postBody) : {}
    const type = body.type ?? 'MEDICAL'
    const service = SERVICE_FOR_TYPE[type] ?? 'AMBULANCE'
    const location = body.location ?? [0, 0]
    const from = [location[0] + 0.012, location[1] - 0.015]
    const { route, cum, distance } = routeBetween(from, location)
    const now = Date.now()
    const incident = {
      id: '00000000-0000-4000-8000-00000000c0de',
      city: id,
      type,
      reported_type: type,
      status: 'DISPATCHED',
      description: body.description ?? null,
      location,
      reported_at: iso(now),
      dispatched_at: iso(now),
      en_route_at: null,
      on_scene_at: null,
      resolved_at: null,
      cancelled_at: null,
      failed_at: null,
      failure_reason: null,
      hospital_station_id: null,
      transport_started_at: null,
      at_hospital_at: null,
    }
    const signals = mutationsOnly ? [] : cityData(id).detail.signals
    return json(
      {
        incident,
        vehicle: {
          id: `${PREFIX[service]}-UICHECK-1`,
          dispatch_id: 'd-uicheck',
          service,
          status: 'DISPATCHED',
          station: {
            id: 'UICHECK',
            name: 'UI check station',
            location: from,
            straight_line_m: metres(from, location),
          },
          route,
          distance_m: distance,
          eta_s: Math.round(distance / 11),
          eta_model: 'kinematic-v3',
          corridor: corridorFor(route, cum, signals),
          route_choice: routeChoiceFor(from, location, 'UICHECK'),
        },
      },
      201,
    )
  }
  if (method === 'DELETE' && (m = p.match(/^\/api\/v1\/cities\/([^/]+)\/incidents\/([^/]+)$/))) {
    return json({ incident: { id: m[2], city: m[1], status: 'CANCELLED' } })
  }
  if (mutationsOnly) return null

  if (p === '/readyz')
    return json({
      ready: true,
      degraded: false,
      checks: {
        postgres: 'ok',
        redis: 'ok',
        worker: { status: 'ok', heartbeat_age_s: 0.2, tick: 10, vehicles: 5 },
      },
    })
  if (p === '/api/v1/cities') return json({ cities: CITIES })
  if ((m = p.match(/^\/api\/v1\/cities\/([^/]+)$/))) {
    if (!CITY_IDS.includes(m[1]))
      return json({ error: { code: 'unknown_city', message: 'unknown city' } }, 404)
    return json(cityData(m[1]).detail)
  }
  if ((m = p.match(/^\/api\/v1\/cities\/([^/]+)\/incidents$/))) {
    const data = cityData(m[1])
    let items = data.incidents
    const st = u.searchParams.getAll('status').flatMap((s) => s.split(','))
    const ty = u.searchParams.getAll('type').flatMap((s) => s.split(','))
    if (st.length) items = items.filter((i) => st.includes(i.status))
    if (ty.length) items = items.filter((i) => ty.includes(i.type))
    const limit = Number(u.searchParams.get('limit') ?? 50)
    const offset = Number(u.searchParams.get('cursor') ?? 0)
    const page = items.slice(offset, offset + limit)
    const next = offset + limit < items.length ? String(offset + limit) : null
    return json({ items: page, next_cursor: next })
  }
  if ((m = p.match(/^\/api\/v1\/incidents\/([^/]+)(\/track)?$/))) {
    for (const cid of CITY_IDS) {
      const data = cityData(cid)
      const inc = data.incidents.find((i) => i.id === m[1])
      if (inc) {
        const d = incidentDetail(inc, data)
        return json(m[2] ? d.track : d.detail)
      }
    }
    return json({ error: { code: 'not_found', message: 'no such incident' } }, 404)
  }
  if ((m = p.match(/^\/api\/v1\/cities\/([^/]+)\/vehicles$/))) {
    const data = cityData(m[1])
    return json({
      city: m[1],
      tick: 10,
      sim_ts: iso(Date.now()),
      vehicles: data.vehicles.map(publicVehicle),
    })
  }
  if ((m = p.match(/^\/api\/v1\/cities\/([^/]+)\/signals\/active$/)))
    return json({ city: m[1], signals: cityData(m[1]).activeSignals })
  if ((m = p.match(/^\/api\/v1\/cities\/([^/]+)\/analytics\/summary$/)))
    return json(cityData(m[1]).analytics)
  return json({ error: { code: 'not_found', message: p } }, 404)
}

/** The active-signal set at `now` (the recovering countdown keeps counting from 14 s). */
function signalsAt(data, now) {
  return data.activeSignals.map((s) =>
    s.state === 'RECOVERING'
      ? { ...s, phase_info: { ...s.phase_info, recovering_until: iso(now + 14_000) } }
      : s,
  )
}

/** Mocked `/cities/{city}/stream`: a snapshot, then a tick every 500 ms; answers pings. */
export function streamHandler(ws) {
  const m = new URL(ws.url()).pathname.match(/\/cities\/([^/]+)\/stream/)
  const id = m?.[1] ?? 'delhi'
  if (!CITY_IDS.includes(id)) {
    ws.close({ code: 4404, reason: 'unknown city' })
    return
  }
  const data = cityData(id)
  let tick = 10
  ws.send(
    JSON.stringify({
      type: 'snapshot',
      v: 1,
      city: id,
      tick,
      sim_ts: iso(Date.now()),
      vehicles: data.vehicles.map(publicVehicle),
      signals: signalsAt(data, Date.now()),
    }),
  )
  const timer = setInterval(() => {
    tick += 1
    const now = Date.now()
    const vehicles = data.vehicles.map((v) => {
      if (v.status === 'EN_ROUTE') v.along_m = Math.min(v.distance_m - 50, v.along_m + 5.75)
      v.next_movement = nextMovementOf(v)
      const pos = posAlong(v.route, v._cum, v.along_m)
      const { route: _r, corridor: _c, _cum: _k, ...rest } = v
      return { ...rest, pos, eta_s: Math.max(0, Math.round((v.distance_m - v.along_m) / 11.5)) }
    })
    try {
      ws.send(
        JSON.stringify({
          type: 'tick',
          v: 1,
          city: id,
          tick,
          sim_ts: iso(now),
          ts: iso(now),
          vehicles,
          signals: signalsAt(data, now),
        }),
      )
    } catch {
      clearInterval(timer)
    }
  }, 500)
  ws.onMessage((msg) => {
    try {
      if (JSON.parse(String(msg)).type === 'ping') ws.send(JSON.stringify({ type: 'pong', v: 1 }))
    } catch {
      // ignore
    }
  })
  ws.onClose(() => clearInterval(timer))
}
