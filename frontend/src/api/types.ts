/**
 * Wire types of the DIVERT v1 API. Mirrors docs/api.md (the normative contract) and the
 * backend serialisers exactly. Coordinates are always `[lat, lng]`; times are ISO 8601 with
 * offset; durations are seconds (`*_s`), distances metres (`*_m`), speeds m/s (`*_mps`).
 * Clients must ignore unknown fields, so every object type is open for extension.
 */

export type LatLng = readonly [lat: number, lng: number]
/** `[south, west, north, east]` */
export type BBox = readonly [south: number, west: number, north: number, east: number]
export type IsoTime = string

export const CITY_IDS = ['delhi', 'kolkata', 'bengaluru'] as const
export type CityId = (typeof CITY_IDS)[number] | (string & {})

export const INCIDENT_TYPES = ['MEDICAL', 'FIRE', 'CRIME'] as const
export type IncidentType = (typeof INCIDENT_TYPES)[number]

export const SERVICE_TYPES = ['AMBULANCE', 'FIRE_ENGINE', 'POLICE'] as const
export type ServiceType = (typeof SERVICE_TYPES)[number]

export const INCIDENT_STATUSES = [
  'OPEN',
  'DISPATCHED',
  'EN_ROUTE',
  'ON_SCENE',
  'RESOLVED',
  'CANCELLED',
  'FAILED',
] as const
export type IncidentStatus = (typeof INCIDENT_STATUSES)[number]
export const ACTIVE_STATUSES: readonly IncidentStatus[] = [
  'OPEN',
  'DISPATCHED',
  'EN_ROUTE',
  'ON_SCENE',
]
export const TERMINAL_STATUSES: readonly IncidentStatus[] = ['RESOLVED', 'CANCELLED', 'FAILED']

export type VehicleStatus = 'EN_ROUTE' | 'ON_SCENE'
export type SignalState = 'NORMAL' | 'PREEMPTED' | 'CONFLICT'
/** Lane advisory (India drives on the left): `L` left turn; `R` straight / right / U-turn. */
export type Lane = 'L' | 'R'

export const SERVICE_FOR_TYPE: Readonly<Record<IncidentType, ServiceType>> = {
  MEDICAL: 'AMBULANCE',
  FIRE: 'FIRE_ENGINE',
  CRIME: 'POLICE',
}

// ------------------------------------------------------------------------ errors
export interface ApiErrorBody {
  error: { code: string; message: string; details?: Record<string, unknown> }
  request_id?: string
}

// ------------------------------------------------------------------------ ops
export interface WorkerCheck {
  status: 'ok' | 'stale' | 'missing' | (string & {})
  heartbeat_age_s?: number | null
  instance?: string
  tick?: number
  vehicles?: number
}
export interface ReadyResponse {
  ready: boolean
  degraded: boolean
  checks: { postgres: string; redis: string; worker: WorkerCheck | string }
}

// ------------------------------------------------------------------------ cities
export interface CitySummary {
  id: CityId
  name: string
  timezone: string
  center: LatLng
  bbox: BBox
  zoom: number
}
export interface CitiesResponse {
  cities: CitySummary[]
}
export interface Station {
  id: string
  name: string | null
  service: ServiceType
  location: LatLng
  osm: string
  total_units: number
  free_units: number
}
export interface Signal {
  id: string
  name: string | null
  location: LatLng
  osm_node_ids: number[]
}
export interface CityDetail extends CitySummary {
  traffic: { hourly_congestion: number[]; avg_junction_delay_s: number }
  stations: Station[]
  signals: Signal[]
}

// ------------------------------------------------------------------------ incidents
export interface IncidentCreate {
  type: string
  location: LatLng
  description?: string | null
}
export interface Incident {
  id: string
  city: CityId
  type: IncidentType
  reported_type: string
  status: IncidentStatus
  description: string | null
  location: LatLng
  reported_at: IsoTime
  dispatched_at: IsoTime | null
  en_route_at: IsoTime | null
  on_scene_at: IsoTime | null
  resolved_at: IsoTime | null
  cancelled_at: IsoTime | null
  failed_at: IsoTime | null
  failure_reason: string | null
}
export interface CorridorEntry {
  signal_id: string
  name: string | null
  pos: LatLng
  along_m: number
  offset_m: number
  lane: Lane
  approach_bearing: number
}
export interface DispatchedVehicle {
  id: string
  dispatch_id: string
  service: ServiceType
  status: 'DISPATCHED'
  station: { id: string; name: string | null; location: LatLng; straight_line_m: number }
  route: LatLng[]
  distance_m: number
  eta_s: number
  eta_model: string
  corridor: CorridorEntry[]
}
export interface IncidentCreated {
  incident: Incident
  vehicle: DispatchedVehicle
}
export interface IncidentCancelled {
  incident: Incident
}

// ------------------------------------------------------------------------ history
export interface IncidentPage {
  items: Incident[]
  next_cursor: string | null
}
export interface IncidentFilters {
  status?: readonly IncidentStatus[]
  type?: readonly IncidentType[]
  from?: string
  to?: string
  limit?: number
  cursor?: string | null
}
export interface DispatchRecord {
  id: string
  vehicle_id: string
  service: ServiceType
  station: { id: string; name: string | null; location: LatLng }
  route: LatLng[]
  distance_m: number
  predicted_eta_s: number
  actual_travel_s: number | null
  est_time_saved_s: number | null
  eta_model: string | null
  corridor: CorridorEntry[] | null
  dispatched_at: IsoTime | null
  arrived_at: IsoTime | null
  ended_at: IsoTime | null
  end_reason: string | null
}
export interface PreemptionRecord {
  event_id: string
  dispatch_id: string
  signal_id: string
  state: Exclude<SignalState, 'NORMAL'>
  lane: Lane | null
  approach_bearing: number | null
  eta_at_claim_s: number | null
  claimed_at: IsoTime | null
  released_at: IsoTime | null
}
export type TimelineType =
  | 'reported'
  | 'dispatched'
  | 'cancel_requested'
  | 'en_route'
  | 'preemption_claimed'
  | 'preemption_conflict'
  | 'preemption_released'
  | 'arrived'
  | 'cleared'
  | 'cancelled'
  | 'failed'
export interface TimelineEvent {
  event_id: string
  dispatch_id: string | null
  type: TimelineType | (string & {})
  ts: IsoTime
  payload: Record<string, unknown> | null
}
export interface IncidentDetail extends Incident {
  dispatches: DispatchRecord[]
  preemptions: PreemptionRecord[]
  timeline: TimelineEvent[]
}
export interface TrackPoint {
  dispatch_id: string
  ts: IsoTime
  pos: LatLng
  speed_mps: number | null
  along_m: number | null
}
export interface TrackResponse {
  incident_id: string
  points: TrackPoint[]
}

// ------------------------------------------------------------------------ analytics
export interface ResponseTimeStats {
  count: number
  p50_s: number | null
  p90_s: number | null
  mean_s: number | null
}
export interface AnalyticsSummary {
  city: CityId
  from: IsoTime
  to: IsoTime
  incidents: {
    total: number
    by_type: Partial<Record<IncidentType, number>>
    by_status: Partial<Record<IncidentStatus, number>>
  }
  response_time_s: Partial<Record<IncidentType, ResponseTimeStats>>
  dispatches: {
    total: number
    arrived: number
    avg_predicted_eta_s: number | null
    avg_actual_travel_s: number | null
    eta_mape: number | null
  }
  preemptions: {
    total: number
    conflicts: number
    distinct_signals: number
    top_signals: { signal_id: string; count: number }[]
  }
  est_time_saved_s: { total: number; note: string }
}

// ------------------------------------------------------------------------ live (Redis)
/** Per-vehicle fields of a `tick` message. */
export interface TickVehicle {
  id: string
  dispatch_id: string
  incident_id: string
  service: ServiceType
  status: VehicleStatus
  pos: LatLng
  heading: number
  speed_mps: number
  eta_s: number
  along_m: number
  distance_m: number
  /** Corridor junctions passed while pre-empted for this vehicle. */
  preempted_passed?: number
}
/** `vehicle_added.vehicle`: tick fields plus static dispatch facts. */
export interface AddedVehicle extends TickVehicle {
  station_id?: string
  incident_type?: IncidentType
  predicted_eta_s?: number
  started_at?: IsoTime
}
/** `GET /vehicles` item (and `snapshot.vehicles`, which always include route + corridor). */
export interface LiveVehicleDoc extends AddedVehicle {
  station_id: string
  incident_type: IncidentType
  predicted_eta_s: number
  elapsed_s: number
  started_at: IsoTime
  destination: LatLng
  claims: string[]
  route?: LatLng[]
  corridor?: CorridorEntry[]
}
export interface VehiclesResponse {
  city: CityId
  tick: number
  sim_ts: IsoTime | null
  vehicles: LiveVehicleDoc[]
}
export interface Claimant {
  vehicle_id: string
  service: ServiceType
  eta_s: number
  state: Exclude<SignalState, 'NORMAL'>
}
export interface ActiveSignal {
  signal_id: string
  state: Exclude<SignalState, 'NORMAL'>
  pos: LatLng
  vehicle_id: string
  service: ServiceType
  lane: Lane
  approach_bearing: number
  eta_s: number
  claimants: Claimant[]
}
export interface ActiveSignalsResponse {
  city: CityId
  signals: ActiveSignal[]
}

// ------------------------------------------------------------------------ WebSocket
interface Envelope<T extends string> {
  type: T
  v: number
}
export interface SnapshotMessage extends Envelope<'snapshot'> {
  city: CityId
  tick: number
  sim_ts: IsoTime | null
  vehicles: LiveVehicleDoc[]
  signals: ActiveSignal[]
}
export interface TickMessage extends Envelope<'tick'> {
  city: CityId
  tick: number
  sim_ts: IsoTime
  ts: IsoTime
  vehicles: TickVehicle[]
  /** The complete non-NORMAL set; a signal missing from it is NORMAL. */
  signals: ActiveSignal[]
}
export interface VehicleAddedMessage extends Envelope<'vehicle_added'> {
  city: CityId
  vehicle: AddedVehicle
  route: LatLng[]
  corridor: CorridorEntry[]
  destination: LatLng
}
export interface VehicleRemovedMessage extends Envelope<'vehicle_removed'> {
  city: CityId
  vehicle_id: string
  dispatch_id: string
  incident_id: string
  reason: 'cleared' | 'cancelled' | 'failed' | (string & {})
}
export interface IncidentUpdatedMessage extends Envelope<'incident_updated'> {
  city: CityId
  incident_id: string
  dispatch_id: string | null
  vehicle_id: string | null
  status: IncidentStatus
  ts: IsoTime
  incident?: Incident
  eta_s?: number
  travel_s?: number
  reason?: string
}
export type PongMessage = Envelope<'pong'>

export type StreamMessage =
  | SnapshotMessage
  | TickMessage
  | VehicleAddedMessage
  | VehicleRemovedMessage
  | IncidentUpdatedMessage
  | PongMessage

export const STREAM_SCHEMA_VERSION = 1
/** WebSocket close codes used by the server. */
export const WS_CLOSE_UNKNOWN_CITY = 4404
export const WS_CLOSE_TRY_AGAIN = 1013
