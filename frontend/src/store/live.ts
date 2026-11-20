import { create } from 'zustand'
import type { ConnectionInfo } from '../api/stream'
import type {
  ActiveSignal,
  AddedVehicle,
  CorridorEntry,
  IncidentStatus,
  IncidentType,
  LatLng,
  LiveVehicleDoc,
  StreamMessage,
  TickVehicle,
} from '../api/types'
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
  route?: LatLng[]
  /** Cumulative haversine distance of `route` (cached once per vehicle). */
  cum?: number[]
  corridor?: CorridorEntry[]
  destination?: LatLng
  motion: Motion
}

export interface LiveIncidentStatus {
  status: IncidentStatus
  ts: string
  vehicle_id: string | null
  eta_s?: number
  travel_s?: number
  reason?: string
}

export interface FeedItem {
  id: number
  ts: string
  kind: 'added' | 'removed' | 'status'
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

function initialMotion(v: TickVehicle, now: number): Motion {
  return {
    fromAlong: v.along_m,
    toAlong: v.along_m,
    fromPos: v.pos,
    toPos: v.pos,
    t0: now,
    durMs: 0,
  }
}

function withRoute<T extends Partial<LiveVehicle>>(
  base: T,
  route: readonly LatLng[] | undefined,
  corridor: readonly CorridorEntry[] | undefined,
  destination: LatLng | undefined,
): T {
  const out: T = { ...base }
  if (route && route.length > 0) {
    out.route = [...route]
    out.cum = cumulativeDistances(route)
  }
  if (corridor) out.corridor = [...corridor]
  if (destination) out.destination = destination
  return out
}

function fromDoc(doc: LiveVehicleDoc, now: number): LiveVehicle {
  return withRoute(
    {
      ...pickTick(doc),
      station_id: doc.station_id,
      incident_type: doc.incident_type,
      predicted_eta_s: doc.predicted_eta_s,
      started_at: doc.started_at,
      motion: initialMotion(doc, now),
    },
    doc.route,
    doc.corridor,
    doc.destination,
  )
}

function pickTick(v: TickVehicle): TickVehicle {
  return {
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
  }
}

function pickStatic(v: AddedVehicle): Partial<LiveVehicle> {
  const out: Partial<LiveVehicle> = {}
  if (v.station_id !== undefined) out.station_id = v.station_id
  if (v.incident_type !== undefined) out.incident_type = v.incident_type
  if (v.predicted_eta_s !== undefined) out.predicted_eta_s = v.predicted_eta_s
  if (v.started_at !== undefined) out.started_at = v.started_at
  return out
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
      if (!cur) continue
      vehicles[d.id] = withRoute(
        {
          ...cur,
          station_id: d.station_id,
          incident_type: d.incident_type,
          predicted_eta_s: d.predicted_eta_s,
          started_at: d.started_at,
        },
        cur.route ? undefined : d.route,
        cur.corridor ? undefined : d.corridor,
        cur.destination ? undefined : d.destination,
      )
      vehicles[d.id] = {
        ...(vehicles[d.id] as LiveVehicle),
        motion: { ...cur.motion, t0: Math.min(cur.motion.t0, now) },
      }
    }
    set({ vehicles })
  },

  apply: (msg, now = performance.now()) => {
    const s = get()
    if ('city' in msg && s.city !== null && msg.city !== s.city) return // another city's stream
    switch (msg.type) {
      case 'snapshot': {
        const vehicles: Record<string, LiveVehicle> = {}
        for (const d of msg.vehicles) vehicles[d.id] = fromDoc(d, now)
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
        const vehicles: Record<string, LiveVehicle> = {}
        for (const tv of msg.vehicles) {
          const cur = s.vehicles[tv.id]
          if (!cur) {
            vehicles[tv.id] = { ...pickTick(tv), motion: initialMotion(tv, now) }
            continue
          }
          const pose = vehiclePose(cur, now)
          // A backwards jump (restore after failover) snaps instead of animating.
          const snap = tv.along_m < pose.along - 1 || tv.dispatch_id !== cur.dispatch_id
          vehicles[tv.id] = {
            ...cur,
            ...pickTick(tv),
            motion: {
              fromAlong: snap ? tv.along_m : pose.along,
              toAlong: tv.along_m,
              fromPos: snap ? tv.pos : pose.pos,
              toPos: tv.pos,
              t0: now,
              durMs: tickIntervalMs,
            },
          }
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
        const cur = s.vehicles[msg.vehicle.id]
        const base: LiveVehicle = {
          ...(cur ?? {}),
          ...pickTick(msg.vehicle),
          ...pickStatic(msg.vehicle),
          motion: cur?.motion ?? initialMotion(msg.vehicle, now),
        }
        set({
          vehicles: {
            ...s.vehicles,
            [msg.vehicle.id]: withRoute(
              { ...base, route: undefined, cum: undefined },
              msg.route,
              msg.corridor,
              msg.destination,
            ),
          },
          feed: pushFeed(s.feed, {
            ts: new Date().toISOString(),
            kind: 'added',
            text: `${msg.vehicle.id} rolling`,
            tone: 'info',
          }),
        })
        return
      }
      case 'vehicle_removed': {
        const vehicles = Object.fromEntries(
          Object.entries(s.vehicles).filter(([id]) => id !== msg.vehicle_id),
        )
        const tone = msg.reason === 'cleared' ? 'good' : msg.reason === 'failed' ? 'bad' : 'warn'
        set({
          vehicles,
          feed: pushFeed(s.feed, {
            ts: new Date().toISOString(),
            kind: 'removed',
            text: `${msg.vehicle_id} ${msg.reason}`,
            tone,
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
        if (msg.reason !== undefined) entry.reason = msg.reason
        const tone =
          msg.status === 'FAILED'
            ? 'bad'
            : msg.status === 'RESOLVED' || msg.status === 'ON_SCENE'
              ? 'good'
              : msg.status === 'CANCELLED'
                ? 'warn'
                : 'info'
        set({
          incidentStatus: { ...s.incidentStatus, [msg.incident_id]: entry },
          feed: pushFeed(s.feed, {
            ts: msg.ts,
            kind: 'status',
            text: `Incident ${msg.incident_id.slice(0, 8)} → ${msg.status.replace('_', ' ').toLowerCase()}`,
            tone,
          }),
        })
        return
      }
      case 'pong':
        return
    }
  },
}))

/** Vehicles that arrived through a tick without their route (e.g. after a resync). */
export function vehiclesMissingRoute(state: Pick<LiveState, 'vehicles'>): string[] {
  return Object.values(state.vehicles)
    .filter((v) => !v.route)
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
