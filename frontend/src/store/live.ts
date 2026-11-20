import { create } from 'zustand'
import type { ConnectionInfo } from '../api/stream'
import type {
  ActiveSignal,
  AddedVehicle,
  CorridorEntry,
  HospitalInfo,
  IncidentStatus,
  IncidentType,
  LatLng,
  LegChangedMessage,
  LiveVehicleDoc,
  Movement,
  RouteReplacedMessage,
  StreamMessage,
  TickVehicle,
} from '../api/types'
import { formatSeconds, LEG_LABEL, STATUS_LABEL } from '../lib/format'
import { cumulativeDistances, interpolateAlong } from '../lib/geo'

/** Where a vehicle is drawn between two ticks (positions are interpolated along the route). */
export interface Motion {
  fromAlong: number
  toAlong: number
  fromPos: LatLng
  toPos: LatLng
  /** `performance.now()` when the latest tick arrived. */
  t0: number
  /** Expected time to the next tick (measured). */
  durMs: number
}

export interface LiveVehicle extends TickVehicle {
  station_id?: string
  incident_type?: IncidentType
  predicted_eta_s?: number
  started_at?: string
  /** Route of the current leg. */
  route?: LatLng[]
  /** Cumulative haversine distance of `route` (cached once per route). */
  cum?: number[]
  corridor?: CorridorEntry[]
  /** Leg `seq` that `route` belongs to (undefined: unknown, e.g. an older server). */
  routeSeq?: number
  hospital?: HospitalInfo | null
  plan_error?: string | null
  incident_location?: LatLng
  home?: LatLng
  dwell_target_s?: number
  reroutes?: number
  /** Sim time (epoch ms) when the current stop (scene / hospital / planning) began. */
  dwellSinceSim?: number
  motion: Motion
}

export interface LiveIncidentStatus {
  status: IncidentStatus
  ts: string
  vehicle_id: string | null
  eta_s?: number
  travel_s?: number
  hospital?: HospitalInfo | null
  reason?: string
}

export interface FeedItem {
  id: number
  ts: string
  kind: 'added' | 'removed' | 'status' | 'leg' | 'reroute'
  text: string
  tone: 'info' | 'good' | 'warn' | 'bad'
}

export interface LiveState {
  city: string | null
  connection: ConnectionInfo
  tick: number
  simTs: string | null
  lastTickAt: number | null
  tickIntervalMs: number
  vehicles: Record<string, LiveVehicle>
  signals: Record<string, ActiveSignal>
  incidentStatus: Record<string, LiveIncidentStatus>
  feed: FeedItem[]

  reset: (city: string | null) => void
  setConnection: (info: ConnectionInfo) => void
  apply: (msg: StreamMessage, now?: number) => void
  mergeRoutes: (docs: readonly LiveVehicleDoc[]) => void
}

const DEFAULT_TICK_MS = 500
const FEED_MAX = 40
let feedSeq = 0

/** Distance along the route at time `now` (linear between the last two ticks). */
export function motionAlong(m: Motion, now: number): number {
  const t = m.durMs > 0 ? Math.min(1, Math.max(0, (now - m.t0) / m.durMs)) : 1
  return m.fromAlong + (m.toAlong - m.fromAlong) * t
}

/** Position + heading to draw at `now`. */
export function vehiclePose(
  v: LiveVehicle,
  now: number,
): { pos: LatLng; heading: number; along: number } {
  const along = motionAlong(v.motion, now)
  if (v.route && v.cum && v.route.length > 1) {
    const p = interpolateAlong(v.route, v.cum, along)
    return { pos: p.pos, heading: p.heading, along }
  }
  const m = v.motion
  const t = m.durMs > 0 ? Math.min(1, Math.max(0, (now - m.t0) / m.durMs)) : 1
  return {
    pos: [
      m.fromPos[0] + (m.toPos[0] - m.fromPos[0]) * t,
      m.fromPos[1] + (m.toPos[1] - m.fromPos[1]) * t,
    ],
    heading: v.heading,
    along,
  }
}

function initialMotion(v: Pick<TickVehicle, 'along_m' | 'pos'>, now: number): Motion {
  return {
    fromAlong: v.along_m,
    toAlong: v.along_m,
    fromPos: v.pos,
    toPos: v.pos,
    t0: now,
    durMs: 0,
  }
}

/** Same polyline (cheap check): keeps the array identity so the map does not redraw it. */
function sameRoute(a: readonly LatLng[] | undefined, b: readonly LatLng[]): boolean {
  if (a?.length !== b.length) return false
  for (const i of [0, a.length >> 1, a.length - 1]) {
    const p = a[i]
    const q = b[i]
    if (p?.[0] !== q?.[0] || p?.[1] !== q?.[1]) return false
  }
  return true
}

function withRoute<T extends Partial<LiveVehicle>>(
  base: T,
  route: readonly LatLng[] | undefined,
  corridor: readonly CorridorEntry[] | undefined,
  destination: LatLng | undefined,
): T {
  const out: T = { ...base }
  if (route && route.length > 0 && !sameRoute(base.route, route)) {
    out.route = [...route]
    out.cum = cumulativeDistances(route)
  }
  if (corridor) out.corridor = [...corridor]
  if (destination) out.destination = destination
  return out
}

/** Copy a value only when the message carries it (older servers omit trip-leg fields). */
function assignDefined<T extends object>(target: T, source: Partial<T>): T {
  for (const [k, v] of Object.entries(source)) {
    if (v !== undefined) (target as Record<string, unknown>)[k] = v
  }
  return target
}

function pickTick(v: TickVehicle): TickVehicle {
  return assignDefined<TickVehicle>(
    {
      id: v.id,
      dispatch_id: v.dispatch_id,
      incident_id: v.incident_id,
      service: v.service,
      status: v.status,
      pos: v.pos,
      heading: v.heading,
      speed_mps: v.speed_mps,
      eta_s: v.eta_s,
      along_m: v.along_m,
      distance_m: v.distance_m,
      preempted_passed: v.preempted_passed ?? 0,
    },
    {
      phase: v.phase,
      leg: v.leg,
      destination: v.destination,
      destination_info: v.destination_info,
      planning: v.planning,
      next_movement: v.next_movement,
      speed_advisory_mps: v.speed_advisory_mps,
    },
  )
}

function pickStatic(v: AddedVehicle): Partial<LiveVehicle> {
  return assignDefined<Partial<LiveVehicle>>(
    {},
    {
      station_id: v.station_id,
      incident_type: v.incident_type,
      predicted_eta_s: v.predicted_eta_s,
      started_at: v.started_at,
    },
  )
}

/** Fields only `/vehicles` and the snapshot carry. */
function pickDocExtras(d: LiveVehicleDoc): Partial<LiveVehicle> {
  return assignDefined<Partial<LiveVehicle>>(
    {},
    {
      hospital: d.hospital,
      plan_error: d.plan_error,
      incident_location: d.incident_location,
      home: d.home,
      dwell_target_s: d.dwell_target_s,
      reroutes: d.reroutes,
    },
  )
}

const simMs = (iso: string | null | undefined): number | null => {
  if (!iso) return null
  const t = Date.parse(iso)
  return Number.isFinite(t) ? t : null
}

function fromDoc(doc: LiveVehicleDoc, now: number, simTs: string | null): LiveVehicle {
  const sim = simMs(simTs)
  const v: LiveVehicle = withRoute(
    {
      ...pickTick(doc),
      ...pickStatic(doc),
      ...pickDocExtras(doc),
      motion: initialMotion(doc, now),
    },
    doc.route,
    doc.corridor,
    doc.destination,
  )
  if (doc.route && doc.leg) v.routeSeq = doc.leg.seq
  if (doc.status === 'ON_SCENE' && sim !== null) v.dwellSinceSim = sim - (doc.dwell_s ?? 0) * 1000
  return v
}

function signalMap(list: readonly ActiveSignal[]): Record<string, ActiveSignal> {
  const out: Record<string, ActiveSignal> = {}
  for (const s of list) out[s.signal_id] = s
  return out
}

function pushFeed(feed: FeedItem[], item: Omit<FeedItem, 'id'>): FeedItem[] {
  feedSeq += 1
  return [{ id: feedSeq, ...item }, ...feed].slice(0, FEED_MAX)
}

/** The route the vehicle holds belongs to an older leg (or dispatch) than it now drives. */
export function routeIsStale(v: Pick<LiveVehicle, 'route' | 'routeSeq' | 'leg'>): boolean {
  if (!v.route) return true
  return v.leg !== undefined && v.routeSeq !== undefined && v.leg.seq !== v.routeSeq
}

/** A tick vehicle merged onto what the store knows (interpolation, stale routes, dwell). */
function tickUpdate(
  cur: LiveVehicle,
  tv: TickVehicle,
  now: number,
  durMs: number,
  sim: number | null,
): LiveVehicle {
  const pose = vehiclePose(cur, now)
  const newDispatch = tv.dispatch_id !== cur.dispatch_id
  const next: LiveVehicle = { ...cur, ...pickTick(tv), motion: cur.motion }
  if (newDispatch) {
    delete next.route
    delete next.cum
    delete next.corridor
    delete next.routeSeq
    delete next.hospital
  }
  const legMismatch = next.route !== undefined && routeIsStale(next)
  if (legMismatch) {
    // a leg change whose leg_changed was missed: never animate along the old leg's route
    delete next.route
    delete next.cum
    delete next.corridor
  }
  // A backwards jump (restore after failover, a new leg) snaps instead of animating.
  const snap = newDispatch || legMismatch || tv.along_m < pose.along - 1
  next.motion = {
    fromAlong: snap ? tv.along_m : pose.along,
    toAlong: tv.along_m,
    fromPos: snap ? tv.pos : pose.pos,
    toPos: tv.pos,
    t0: now,
    durMs,
  }
  if (tv.status === 'ON_SCENE') {
    const restart = cur.status !== 'ON_SCENE' || cur.dwellSinceSim === undefined || newDispatch
    if (restart && sim !== null) next.dwellSinceSim = sim
  } else {
    delete next.dwellSinceSim
  }
  return next
}

function legChanged(cur: LiveVehicle, msg: LegChangedMessage, now: number): LiveVehicle {
  const start = msg.route[0] ?? cur.pos
  const next: LiveVehicle = withRoute(
    {
      ...cur,
      phase: msg.phase,
      leg: msg.leg,
      destination_info: msg.destination_info,
      predicted_eta_s: msg.predicted_eta_s,
      planning: false,
      next_movement: null,
      speed_advisory_mps: null,
      plan_error: null,
      reroutes: 0,
      along_m: 0,
      status: 'EN_ROUTE',
      motion: initialMotion({ along_m: 0, pos: start }, now),
    },
    msg.route,
    msg.corridor,
    msg.destination,
  )
  next.routeSeq = msg.leg.seq
  if (msg.hospital) next.hospital = msg.hospital
  delete next.dwellSinceSim
  return next
}

function routeReplaced(cur: LiveVehicle, msg: RouteReplacedMessage): LiveVehicle {
  const next = withRoute({ ...cur, reroutes: msg.reroute_no }, msg.route, msg.corridor, undefined)
  next.routeSeq = msg.leg.seq
  return next
}

function legText(msg: LegChangedMessage): string {
  const name = msg.destination_info.name ?? msg.destination_info.station_id ?? 'destination'
  return `${msg.vehicle_id} ${LEG_LABEL[msg.leg.kind].toLowerCase()} leg to ${name}`
}

function removedText(id: string, reason: string): { text: string; tone: FeedItem['tone'] } {
  if (reason === 'back_in_service' || reason === 'cleared')
    return { text: `${id} back in service`, tone: 'good' }
  if (reason === 'failed') return { text: `${id} failed`, tone: 'bad' }
  return { text: `${id} ${reason.replace(/_/g, ' ')}`, tone: 'warn' }
}

function statusTone(status: IncidentStatus): FeedItem['tone'] {
  if (status === 'FAILED') return 'bad'
  if (status === 'RESOLVED' || status === 'ON_SCENE' || status === 'AT_HOSPITAL') return 'good'
  if (status === 'CANCELLED') return 'warn'
  return 'info'
}

const initial = {
  city: null,
  connection: { status: 'idle', attempt: 0 } as ConnectionInfo,
  tick: 0,
  simTs: null,
  lastTickAt: null,
  tickIntervalMs: DEFAULT_TICK_MS,
  vehicles: {},
  signals: {},
  incidentStatus: {},
  feed: [],
} satisfies Partial<LiveState>

export const useLiveStore = create<LiveState>()((set, get) => ({
  ...initial,

  reset: (city) => set({ ...initial, city }),

  setConnection: (connection) => set({ connection }),

  mergeRoutes: (docs) => {
    const now = performance.now()
    const vehicles = { ...get().vehicles }
    for (const d of docs) {
      const cur = vehicles[d.id]
      if (cur?.dispatch_id !== d.dispatch_id) continue
      // the REST document trails the stream: never go back to an older leg's route
      if (cur.leg && d.leg && d.leg.seq < cur.leg.seq) continue
      const take = routeIsStale(cur)
      const merged = withRoute(
        { ...cur, ...pickStatic(d), ...pickDocExtras(d) },
        take ? d.route : undefined,
        take || !cur.corridor ? d.corridor : undefined,
        cur.destination ? undefined : d.destination,
      )
      if (take && d.route && d.leg) merged.routeSeq = d.leg.seq
      merged.motion = { ...cur.motion, t0: Math.min(cur.motion.t0, now) }
      vehicles[d.id] = merged
    }
    set({ vehicles })
  },

  apply: (msg, now = performance.now()) => {
    const s = get()
    if ('city' in msg && s.city !== null && msg.city !== s.city) return // another city's stream
    switch (msg.type) {
      case 'snapshot': {
        const vehicles: Record<string, LiveVehicle> = {}
        for (const d of msg.vehicles) vehicles[d.id] = fromDoc(d, now, msg.sim_ts)
        set({
          tick: msg.tick,
          simTs: msg.sim_ts,
          vehicles,
          signals: signalMap(msg.signals),
          lastTickAt: null,
        })
        return
      }
      case 'tick': {
        const measured = s.lastTickAt === null ? DEFAULT_TICK_MS : now - s.lastTickAt
        const tickIntervalMs = Math.min(
          2000,
          Math.max(
            150,
            s.lastTickAt === null ? s.tickIntervalMs : s.tickIntervalMs * 0.7 + measured * 0.3,
          ),
        )
        const sim = simMs(msg.sim_ts)
        const vehicles: Record<string, LiveVehicle> = {}
        for (const tv of msg.vehicles) {
          const cur = s.vehicles[tv.id]
          if (!cur) {
            vehicles[tv.id] = { ...pickTick(tv), motion: initialMotion(tv, now) }
            if (tv.status === 'ON_SCENE' && sim !== null)
              (vehicles[tv.id] as LiveVehicle).dwellSinceSim = sim
            continue
          }
          vehicles[tv.id] = tickUpdate(cur, tv, now, tickIntervalMs, sim)
        }
        set({
          tick: msg.tick,
          simTs: msg.sim_ts,
          lastTickAt: now,
          tickIntervalMs,
          vehicles,
          signals: signalMap(msg.signals),
        })
        return
      }
      case 'vehicle_added': {
        const v = msg.vehicle
        const cur = s.vehicles[v.id]
        const same = cur?.dispatch_id === v.dispatch_id ? cur : undefined
        const sameLeg = same && (v.leg === undefined || same.leg?.seq === v.leg.seq) ? same : null
        const base: LiveVehicle = {
          ...(sameLeg ?? {}),
          ...pickTick(v),
          ...pickStatic(v),
          motion: sameLeg ? sameLeg.motion : initialMotion(v, now),
        }
        const added = withRoute(base, msg.route, msg.corridor, msg.destination)
        if (v.leg) added.routeSeq = v.leg.seq
        const first = same === undefined
        set({
          vehicles: { ...s.vehicles, [v.id]: added },
          feed: first
            ? pushFeed(s.feed, {
                ts: new Date().toISOString(),
                kind: 'added',
                text: `${v.id} rolling`,
                tone: 'info',
              })
            : s.feed,
        })
        return
      }
      case 'leg_changed': {
        const cur = s.vehicles[msg.vehicle_id]
        const feed = pushFeed(s.feed, {
          ts: new Date().toISOString(),
          kind: 'leg',
          text: legText(msg),
          tone: msg.leg.kind === 'RETURN' ? 'info' : 'warn',
        })
        if (cur?.dispatch_id !== msg.dispatch_id) {
          set({ feed }) // vehicle_added follows with the full vehicle
          return
        }
        set({ vehicles: { ...s.vehicles, [cur.id]: legChanged(cur, msg, now) }, feed })
        return
      }
      case 'route_replaced': {
        const cur = s.vehicles[msg.vehicle_id]
        if (cur?.dispatch_id !== msg.dispatch_id) return
        if (cur.leg && cur.leg.seq !== msg.leg.seq) return // an older leg's re-route
        set({
          vehicles: { ...s.vehicles, [cur.id]: routeReplaced(cur, msg) },
          feed: pushFeed(s.feed, {
            ts: new Date().toISOString(),
            kind: 'reroute',
            text: `${msg.vehicle_id} re-routed, saves ${formatSeconds(msg.saved_s)}`,
            tone: 'info',
          }),
        })
        return
      }
      case 'vehicle_removed': {
        const vehicles = Object.fromEntries(
          Object.entries(s.vehicles).filter(([id]) => id !== msg.vehicle_id),
        )
        set({
          vehicles,
          feed: pushFeed(s.feed, {
            ts: new Date().toISOString(),
            kind: 'removed',
            ...removedText(msg.vehicle_id, msg.reason),
          }),
        })
        return
      }
      case 'incident_updated': {
        const entry: LiveIncidentStatus = {
          status: msg.status,
          ts: msg.ts,
          vehicle_id: msg.vehicle_id,
        }
        if (msg.eta_s !== undefined) entry.eta_s = msg.eta_s
        if (msg.travel_s !== undefined) entry.travel_s = msg.travel_s
        if (msg.hospital !== undefined) entry.hospital = msg.hospital
        if (msg.reason !== undefined) entry.reason = msg.reason
        set({
          incidentStatus: { ...s.incidentStatus, [msg.incident_id]: entry },
          feed: pushFeed(s.feed, {
            ts: msg.ts,
            kind: 'status',
            text: `Incident ${msg.incident_id.slice(0, 8)} → ${STATUS_LABEL[msg.status].toLowerCase()}`,
            tone: statusTone(msg.status),
          }),
        })
        return
      }
      case 'pong':
        return
    }
  },
}))

/** Vehicles without a route of their current leg (e.g. after a resync or a missed message). */
export function vehiclesMissingRoute(state: Pick<LiveState, 'vehicles'>): string[] {
  return Object.values(state.vehicles)
    .filter((v) => routeIsStale(v))
    .map((v) => v.id)
}

/** First corridor junction still ahead of the vehicle, with the distance to it. */
export function nextSignal(
  v: Pick<LiveVehicle, 'corridor' | 'along_m'>,
): { entry: CorridorEntry; distance_m: number } | null {
  for (const c of v.corridor ?? []) {
    if (c.along_m > v.along_m) return { entry: c, distance_m: c.along_m - v.along_m }
  }
  return null
}

/** The next junction as the fleet card shows it (server `next_movement`, else the corridor). */
export interface NextJunction {
  signal_id: string
  name: string | null
  distance_m: number
  movement: Movement | null
  lane_index: number | null
  lanes_total: number | null
  side: CorridorEntry['lane']
  text: string | null
  /** Metres from the vehicle to the point where it should be in the target lane (<= 0: now). */
  lane_by_m: number | null
  free_left: boolean
}

export function nextJunction(
  v: Pick<LiveVehicle, 'corridor' | 'along_m' | 'next_movement' | 'status'>,
): NextJunction | null {
  const nm = v.next_movement
  if (nm) {
    const entry = (v.corridor ?? []).find(
      (c) => c.signal_id === nm.signal_id && (c.pass_no ?? 0) === nm.pass_no,
    )
    return {
      signal_id: nm.signal_id,
      name: entry?.name ?? null,
      distance_m: nm.distance_m,
      movement: nm.movement,
      lane_index: nm.lane_index,
      lanes_total: nm.lanes_total,
      side: nm.side,
      text: nm.text,
      lane_by_m: nm.advise_at_m === null ? null : nm.advise_at_m - v.along_m,
      free_left: nm.free_left,
    }
  }
  if (v.next_movement === null || v.status !== 'EN_ROUTE') return null // server: none ahead
  const legacy = nextSignal(v)
  if (!legacy) return null
  const e = legacy.entry
  return {
    signal_id: e.signal_id,
    name: e.name,
    distance_m: legacy.distance_m,
    movement: e.movement ?? null,
    lane_index: e.advice?.lane_index ?? null,
    lanes_total: e.advice?.lanes_total ?? null,
    side: e.lane,
    text: e.advice?.text ?? null,
    lane_by_m: e.advice ? e.advice.advise_at_m - v.along_m : null,
    free_left: e.free_left ?? false,
  }
}

/** Simulated seconds the vehicle has been stopped (scene, hospital, planning), if known. */
export function dwellElapsedS(
  v: Pick<LiveVehicle, 'dwellSinceSim' | 'status'>,
  simTs: string | null,
): number | null {
  const sim = simMs(simTs)
  if (v.status !== 'ON_SCENE' || v.dwellSinceSim === undefined || sim === null) return null
  return Math.max(0, (sim - v.dwellSinceSim) / 1000)
}
