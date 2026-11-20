// Deterministic API + WebSocket mocks for scripts/clickability.mjs (mock mode).
// Shapes follow src/api/types.ts (docs/api.md). Only what the UI renders is filled in.

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
const STATUSES = ['RESOLVED', 'RESOLVED', 'RESOLVED', 'CANCELLED', 'FAILED', 'RESOLVED']

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

function routeBetween(from, to) {
  // An L-shaped "street" route with a few vertices.
  const mid = [from[0], to[1]]
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

function corridorFor(route, cum, signals, offset = 0) {
  return [1, 2, 4, 5].map((i, k) => ({
    signal_id: signals[offset + k]?.id ?? `J${offset + i}`,
    name: k % 2 === 0 ? `${['Ring Road', 'MG Road', 'Park Street', 'ITO'][k]} crossing` : null,
    pos: route[i],
    along_m: Number(cum[i].toFixed(2)),
    offset_m: 2,
    lane: k % 2 === 0 ? 'L' : 'R',
    approach_bearing: k * 90,
  }))
}

const iso = (ms) => new Date(ms).toISOString()

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

  // History: 60 incidents, newest first; the first 3 are active (they also drive the live view).
  const incidents = []
  for (let i = 0; i < 60; i++) {
    const type = TYPES[i % 3]
    const reported = now - i * 37 * 60_000 - 90_000
    const active = i < 3
    const status = active
      ? ['EN_ROUTE', 'DISPATCHED', 'ON_SCENE'][i]
      : STATUSES[i % STATUSES.length]
    const onScene =
      status === 'RESOLVED' || status === 'ON_SCENE' ? reported + (240 + (i % 7) * 31) * 1000 : null
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
            ][i % 4 === 0 ? (i / 4) % 4 : 0]
          : null,
      location: inner(city, r, 0.5),
      reported_at: iso(reported),
      dispatched_at: iso(reported + 2000),
      en_route_at: iso(reported + 4000),
      on_scene_at: onScene ? iso(onScene) : null,
      resolved_at: status === 'RESOLVED' && onScene ? iso(onScene + 600_000) : null,
      cancelled_at: status === 'CANCELLED' ? iso(reported + 120_000) : null,
      failed_at: status === 'FAILED' ? iso(reported + 5000) : null,
      failure_reason: status === 'FAILED' ? 'no_unit_available' : null,
    })
  }

  // Live vehicles for the active incidents.
  const vehicles = incidents.slice(0, 3).map((inc, k) => {
    const service = SERVICE_FOR_TYPE[inc.type]
    const station = stations.find((s) => s.service === service) ?? stations[0]
    const { route, cum, distance } = routeBetween(station.location, inc.location)
    const along = inc.status === 'ON_SCENE' ? distance : distance * (0.25 + 0.2 * k)
    return {
      id: `${PREFIX[service]}-${station.id}-1`,
      dispatch_id: `d-${inc.id.slice(0, 8)}`,
      incident_id: inc.id,
      service,
      station_id: station.id,
      incident_type: inc.type,
      status: inc.status === 'ON_SCENE' ? 'ON_SCENE' : 'EN_ROUTE',
      pos: posAlong(route, cum, along),
      heading: 90,
      speed_mps: inc.status === 'ON_SCENE' ? 0 : 11.5,
      eta_s: inc.status === 'ON_SCENE' ? 0 : Math.round((distance - along) / 11.5),
      predicted_eta_s: Math.round(distance / 11),
      along_m: along,
      distance_m: distance,
      elapsed_s: 60,
      started_at: inc.dispatched_at,
      destination: inc.location,
      claims: [],
      preempted_passed: k + 1,
      route,
      corridor: corridorFor(route, cum, signals, 4 * (k + 1)),
      _cum: cum,
    }
  })

  const activeSignals = vehicles.slice(0, 2).map((v, k) => ({
    signal_id: v.corridor[2].signal_id,
    state: k === 0 ? 'PREEMPTED' : 'CONFLICT',
    pos: v.corridor[2].pos,
    vehicle_id: v.id,
    service: v.service,
    lane: v.corridor[2].lane,
    approach_bearing: v.corridor[2].approach_bearing,
    eta_s: 18 + k * 7,
    claimants: [
      {
        vehicle_id: v.id,
        service: v.service,
        eta_s: 18 + k * 7,
        state: k === 0 ? 'PREEMPTED' : 'CONFLICT',
      },
    ],
  }))

  const analytics = {
    city: id,
    from: iso(now - 7 * 86_400_000),
    to: iso(now),
    incidents: {
      total: 57,
      by_type: { MEDICAL: 25, FIRE: 12, CRIME: 20 },
      by_status: {
        OPEN: 0,
        DISPATCHED: 1,
        EN_ROUTE: 1,
        ON_SCENE: 1,
        RESOLVED: 44,
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
    },
    preemptions: {
      total: 141,
      conflicts: 6,
      distinct_signals: 64,
      top_signals: signals.slice(0, 5).map((s, i) => ({ signal_id: s.id, count: 9 - i })),
    },
    est_time_saved_s: {
      total: 4935,
      note: 'estimate: pre-empted junctions passed x city average junction delay',
    },
  }

  return { city, detail, incidents, vehicles, activeSignals, analytics }
}

const cache = new Map()
const cityData = (id) => {
  if (!cache.has(id)) cache.set(id, buildCity(id))
  return cache.get(id)
}

const publicVehicle = ({ _cum, ...v }) => v

function incidentDetail(inc, data) {
  const service = SERVICE_FOR_TYPE[inc.type]
  const station = data.detail.stations.find((s) => s.service === service) ?? data.detail.stations[0]
  const { route, cum, distance } = routeBetween(station.location, inc.location)
  const corridor = corridorFor(route, cum, data.detail.signals)
  const t0 = Date.parse(inc.dispatched_at)
  const travel = inc.on_scene_at ? (Date.parse(inc.on_scene_at) - t0) / 1000 : null
  const dispatchId = `d-${inc.id.slice(0, 8)}`
  const preemptions = corridor.slice(0, 3).map((c, i) => ({
    event_id: `p${i}`,
    dispatch_id: dispatchId,
    signal_id: c.signal_id,
    state: i === 1 ? 'CONFLICT' : 'PREEMPTED',
    lane: c.lane,
    approach_bearing: c.approach_bearing,
    eta_at_claim_s: 20 + i * 3,
    claimed_at: iso(t0 + (i + 1) * 30_000),
    released_at: iso(t0 + (i + 1) * 30_000 + 25_000),
  }))
  const timeline = [
    ['reported', inc.reported_at],
    ['dispatched', inc.dispatched_at],
    ['en_route', inc.en_route_at],
    ...preemptions.map((p) => [
      p.state === 'CONFLICT' ? 'preemption_conflict' : 'preemption_claimed',
      p.claimed_at,
      { signal_id: p.signal_id, lane: p.lane },
    ]),
    ['arrived', inc.on_scene_at, travel ? { travel_s: travel } : null],
    ['cleared', inc.resolved_at],
    ['cancelled', inc.cancelled_at],
    ['failed', inc.failed_at, inc.failure_reason ? { reason: inc.failure_reason } : null],
  ]
    .filter(([, ts]) => ts)
    .map(([type, ts, payload], i) => ({
      event_id: `e${i}`,
      dispatch_id: dispatchId,
      type,
      ts,
      payload: payload ?? null,
    }))
  const points = route.map((pos, i) => ({
    dispatch_id: dispatchId,
    ts: iso(t0 + i * 20_000),
    pos,
    speed_mps: 11,
    along_m: cum[i],
  }))
  return {
    detail: {
      ...inc,
      dispatches: [
        {
          id: dispatchId,
          vehicle_id: `${PREFIX[service]}-${station.id}-1`,
          service,
          station: { id: station.id, name: station.name, location: station.location },
          route,
          distance_m: distance,
          predicted_eta_s: Math.round(distance / 11),
          actual_travel_s: travel,
          est_time_saved_s: 70,
          eta_model: 'osrm+congestion',
          corridor,
          dispatched_at: inc.dispatched_at,
          arrived_at: inc.on_scene_at,
          ended_at: inc.resolved_at ?? inc.cancelled_at ?? inc.failed_at,
          end_reason:
            inc.status === 'RESOLVED' ? 'CLEARED' : inc.status === 'CANCELLED' ? 'CANCELLED' : null,
        },
      ],
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
          eta_model: 'osrm+congestion',
          corridor: corridorFor(route, cum, signals),
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
        worker: { status: 'ok', heartbeat_age_s: 0.2, tick: 10, vehicles: 3 },
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
    const st = u.searchParams.get('status')
    const ty = u.searchParams.get('type')
    if (st) items = items.filter((i) => st.split(',').includes(i.status))
    if (ty) items = items.filter((i) => ty.split(',').includes(i.type))
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
      signals: data.activeSignals,
    }),
  )
  const timer = setInterval(() => {
    tick += 1
    const vehicles = data.vehicles.map((v) => {
      if (v.status === 'EN_ROUTE') v.along_m = Math.min(v.distance_m - 50, v.along_m + 5.75)
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
          sim_ts: iso(Date.now()),
          ts: iso(Date.now()),
          vehicles,
          signals: data.activeSignals,
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
