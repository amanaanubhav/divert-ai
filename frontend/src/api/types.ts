/**
 * Wire types of the DIVERT v1 API. Mirrors docs/api.md (the normative contract) and the
 * backend serialisers exactly. Coordinates are always `[lat, lng]`; times are ISO 8601 with
 * offset; durations are seconds (`*_s`), distances metres (`*_m`), speeds m/s (`*_mps`).
 * Clients must ignore unknown fields, so every object type is open for extension. Fields
 * added with trip legs (design section 5) are optional where an older server omits them.
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
  'TRANSPORTING',
  'AT_HOSPITAL',
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
  'TRANSPORTING',
  'AT_HOSPITAL',
]
export const TERMINAL_STATUSES: readonly IncidentStatus[] = ['RESOLVED', 'CANCELLED', 'FAILED']
/** A patient is on board: `DELETE` answers 409 `cannot_cancel_transport`. */
export const TRANSPORT_STATUSES: readonly IncidentStatus[] = ['TRANSPORTING', 'AT_HOSPITAL']

/** Legacy two-value vehicle status: driving a leg, or stopped. `phase` is the unit status. */
export type VehicleStatus = 'EN_ROUTE' | 'ON_SCENE'
/** Per-claim label kept for compatibility: served (PREEMPTED) or queued (CONFLICT). */
export type SignalState = 'NORMAL' | 'PREEMPTED' | 'CONFLICT'
/** Junction state (design section 4.3). */
export const JUNCTION_STATES = [
  'NORMAL',
  'TRANSITIONING',
  'PREEMPTED',
  'QUEUED',
  'RECOVERING',
] as const
export type JunctionState = (typeof JUNCTION_STATES)[number]
/** Lane side (India drives on the left): `L` kerb lane, `C` middle / single lane, `R` median side. */
export type Lane = 'L' | 'C' | 'R'

export const LEG_KINDS = ['RESPONSE', 'TRANSPORT', 'RETURN'] as const
export type LegKind = (typeof LEG_KINDS)[number]
/** Unit status while on a dispatch (`phase` of a live vehicle). */
export const UNIT_PHASES = [
  'DISPATCHED',
  'ON_SCENE',
  'TRANSPORTING',
  'AT_HOSPITAL',
  'RETURNING',
] as const
export type UnitPhase = (typeof UNIT_PHASES)[number]
export const DISPATCH_STATUSES = [
  'DISPATCHED',
  'ON_SCENE',
  'TRANSPORTING',
  'AT_HOSPITAL',
  'RETURNING',
  'COMPLETED',
  'FAILED',
] as const
export type DispatchStatus = (typeof DISPATCH_STATUSES)[number]
export type DestinationKind = 'SCENE' | 'HOSPITAL' | 'STATION'
export type LegEndReason = 'ARRIVED' | 'CANCELLED' | 'FAILED' | 'SUPERSEDED'
export const PREEMPT_OUTCOMES = ['CLEARED', 'TIMEOUT', 'CANCELLED', 'REROUTED'] as const
export type PreemptOutcome = (typeof PREEMPT_OUTCOMES)[number]
export type PhaseStateAtClaim = 'green_extend' | 'red_truncate'
export const MOVEMENTS = [
  'STRAIGHT',
  'SLIGHT_LEFT',
  'LEFT',
  'SHARP_LEFT',
  'SLIGHT_RIGHT',
  'RIGHT',
  'SHARP_RIGHT',
  'U_TURN',
  'ROUNDABOUT',
] as const
export type Movement = (typeof MOVEMENTS)[number]
/** OSM `emergency` tag of a hospital (`null` = not tagged). */
export type EmergencyTag = 'yes' | 'no' | (string & {}) | null

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
  /** Destination hospital (MEDICAL, once transported). Optional: older payloads lack it. */
  hospital_station_id?: string | null
  transport_started_at?: IsoTime | null
  at_hospital_at?: IsoTime | null
}
/** Lane advice at one junction (design section 3; lanes numbered from the left kerb). */
export interface LaneAdvice {
  movement: Movement
  lane_index: number
  lanes_total: number
  side: Lane
  /** Along-route position by which the vehicle should be in the target lane. */
  advise_at_m: number
  free_left: boolean
  road_class?: string
  lookahead?: boolean
  text: string
}
export interface CorridorEntry {
  signal_id: string
  name: string | null
  pos: LatLng
  along_m: number
  offset_m: number
  lane: Lane
  approach_bearing: number
  pass_no?: number
  movement?: Movement
  turn_deg?: number
  exit_bearing?: number | null
  roundabout_exit?: number | null
  /** A slip road bypasses the signal: never claimed, shown as "free left". */
  free_left?: boolean
  legs?: number | null
  junction_bearings?: number[]
  advice?: LaneAdvice
}
/** One scored route candidate (`route_choice.candidates[]`). */
export interface RouteCandidate {
  label: string
  source: 'osrm' | 'detour' | (string & {})
  distance_m: number
  base_time_s: number
  congested_time_s: number
  junction_delay_s: number
  penalties_s: number
  cost_s: number
  crowd_index: number
  signals: number
  right_turns: number
  contention_junctions: string[]
  incident_zones: { incident_id: string; type: IncidentType }[]
  congested_road: string | null
  /**
   * Simplified geometry of the candidate, `[[lat, lng], ...]` (Douglas-Peucker, <= 150 points;
   * docs/api.md), drawn faint on hover. Absent in route choices recorded before it existed.
   */
  route?: LatLng[] | null
}
export interface RouteChoice {
  station_id?: string
  chosen: string
  reason: string
  tie_break: string | null
  candidates: RouteCandidate[]
}
export interface HospitalCandidate {
  station_id: string
  name: string | null
  location: LatLng
  emergency: EmergencyTag
  healthcare: string | null
  straight_line_m: number
  cost_s: number | null
  eta_s: number | null
  distance_m: number | null
  /** Label of the best route candidate to it. */
  route: string | null
  error: string | null
}
export interface HospitalChoice {
  chosen: string
  reason: string
  ed_tolerance_s?: number | null
  candidates: HospitalCandidate[]
}
export interface HospitalInfo {
  id: string
  name: string | null
  location: LatLng
  emergency: EmergencyTag
  healthcare?: string | null
}
export interface DestinationInfo {
  kind: DestinationKind
  station_id: string | null
  name: string | null
  location: LatLng
}
export interface LegRef {
  seq: number
  kind: LegKind
  emergency: boolean
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
  route_choice?: RouteChoice | null
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
  /** Status of the whole trip (absent on pre-legs payloads). */
  status?: DispatchStatus
  station: { id: string; name: string | null; location: LatLng }
  /** The RESPONSE leg (compatibility); every leg is in `IncidentDetail.legs`. */
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
  route_choice?: RouteChoice | null
}
/** One leg of a dispatch (`legs[]` of the incident detail). */
export interface LegRecord {
  dispatch_id: string
  seq: number
  kind: LegKind
  emergency: boolean
  origin: LatLng
  destination: DestinationInfo
  route: LatLng[]
  distance_m: number
  predicted_eta_s: number
  /** Simulated seconds. */
  actual_travel_s: number | null
  /** (actual - predicted) / actual x 100. */
  eta_error_pct: number | null
  eta_model: string | null
  planned_at: IsoTime | null
  started_at: IsoTime | null
  ended_at: IsoTime | null
  end_reason: LegEndReason | null
  reroutes: number
  /** Simulated seconds since the dispatch started. */
  started_trip_s: number | null
  ended_trip_s: number | null
  corridor: CorridorEntry[] | null
  route_choice: RouteChoice | null
  /** TRANSPORT only. */
  hospital_choice: HospitalChoice | null
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
  leg_seq?: number | null
  movement?: Movement | null
  lane_index?: number | null
  lanes_total?: number | null
  free_left?: boolean | null
  required_lead_s?: number | null
  transition_s?: number | null
  queue_clear_s?: number | null
  /** Simulation clock (may differ from wall time when the time scale is not 1). */
  green_at?: IsoTime | null
  crossed_at?: IsoTime | null
  waited_s?: number | null
  outcome?: PreemptOutcome | null
  phase_state_at_claim?: PhaseStateAtClaim | null
}
export type TimelineType =
  | 'reported'
  | 'dispatched'
  | 'cancel_requested'
  | 'en_route'
  | 'preemption_claimed'
  | 'preemption_conflict'
  | 'preempt_transition'
  | 'preempt_green'
  | 'preempt_timeout'
  | 'preemption_released'
  | 'arrived'
  | 'cleared'
  | 'leg_planned'
  | 'leg_plan_failed'
  | 'leg_started'
  | 'leg_ended'
  | 'rerouted'
  | 'transport_started'
  | 'arrived_hospital'
  | 'handover_complete'
  | 'returning'
  | 'back_in_service'
  | 'cancelled'
  | 'cancel_rejected'
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
  /** Every leg of every dispatch, by (dispatch, seq). Absent on pre-legs payloads. */
  legs?: LegRecord[]
  hospital?: HospitalInfo | null
}
export interface TrackPoint {
  dispatch_id: string
  ts: IsoTime
  pos: LatLng
  speed_mps: number | null
  /** Along the leg `leg_seq` (each leg starts at 0). */
  along_m: number | null
  leg_seq?: number
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
export interface LegAccuracy {
  count: number
  eta_mape: number | null
  eta_max_error: number | null
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
    by_status?: Partial<Record<DispatchStatus, number>>
  }
  /** Trip-leg metrics (absent on older servers). Simulated seconds. */
  transport_time_s?: ResponseTimeStats
  call_to_hospital_s?: ResponseTimeStats
  handover_to_back_in_service_s?: ResponseTimeStats
  hospitals?: { station_id: string; name: string | null; count: number }[]
  legs?: Partial<Record<LegKind, LegAccuracy>>
  free_left_passed?: number
  reroutes?: number
  preemptions: {
    total: number
    conflicts: number
    distinct_signals: number
    top_signals: { signal_id: string; count: number }[]
    outcomes?: Partial<Record<PreemptOutcome, number>>
    green_extension?: number
    red_truncation?: number
  }
  est_time_saved_s: { total: number; note: string }
}

// ------------------------------------------------------------------------ live (Redis)
/** The next junction ahead of a driving vehicle, with its movement and lane advice. */
export interface NextMovement {
  signal_id: string
  pass_no: number
  movement: Movement
  lane_index: number | null
  lanes_total: number | null
  side: Lane
  text: string | null
  /** Along-route position by which to be in the target lane. */
  advise_at_m: number | null
  /** Distance from the vehicle to the junction. */
  distance_m: number
  free_left: boolean
}
/** Per-vehicle fields of a `tick` message (trip-leg fields optional for older servers). */
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
  /** Corridor junctions passed while pre-empted for this vehicle (whole trip). */
  preempted_passed?: number
  phase?: UnitPhase
  leg?: LegRef
  /** End of the current leg (v1 type). */
  destination?: LatLng
  destination_info?: DestinationInfo
  /** Stopped, dwell over, waiting for the next leg's plan ("planning route"). */
  planning?: boolean
  next_movement?: NextMovement | null
  /** Set while the vehicle must slow for its green (0 = stop at the stop line). */
  speed_advisory_mps?: number | null
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
  plan_error?: string | null
  incident_location?: LatLng
  home?: LatLng
  hospital?: HospitalInfo | null
  dwell_s?: number
  dwell_target_s?: number
  trip_s?: number
  reroutes?: number
  emergency?: boolean
  leg_kind?: LegKind
  rank?: number
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
  /** Legacy per-vehicle label: served (PREEMPTED) or queued (CONFLICT). */
  state: Exclude<SignalState, 'NORMAL'>
  /** The claimant's own view: TRANSITIONING / PREEMPTED (served) or QUEUED. */
  phase_state?: JunctionState
  movement?: Movement
  /** Conflict priority: 0 ambulance with a patient, 1 ambulance, 2 fire, 3 police. */
  rank?: number
  lane?: Lane
  lane_index?: number | null
  lanes_total?: number | null
  phase?: number | null
  queue_pos?: number | null
  expected_green_at?: IsoTime | null
}
export interface PhaseInfo {
  served_phase: number | null
  phases: number
  cycle_s: number
  geometry: string
  /** Simulated seconds until the served phase is green (TRANSITIONING). */
  transition_s_left: number
  expected_green_at: IsoTime | null
  /** Simulated seconds the served phase has been held green. */
  hold_s: number
  state_at_claim?: PhaseStateAtClaim | null
  /** RECOVERING only. */
  recovery_phase?: number | null
  recovering_until?: IsoTime | null
}
export interface ActiveSignal {
  signal_id: string
  /** Junction state; `CONFLICT` only from pre-stage-A servers (shown as QUEUED). */
  state: Exclude<JunctionState, 'NORMAL'> | 'CONFLICT'
  pos: LatLng
  vehicle_id: string
  service: ServiceType
  lane: Lane
  approach_bearing: number
  eta_s: number
  claimants: Claimant[]
  phase_info?: PhaseInfo
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
/** A TRANSPORT / RETURN leg starts (published before that tick, with `vehicle_added`). */
export interface LegChangedMessage extends Envelope<'leg_changed'> {
  city: CityId
  vehicle_id: string
  dispatch_id: string
  incident_id: string
  phase: UnitPhase
  leg: LegRef
  destination: LatLng
  destination_info: DestinationInfo
  predicted_eta_s: number
  route: LatLng[]
  corridor: CorridorEntry[]
  hospital: HospitalInfo | null
}
/** A re-route was applied: `route` is the whole spliced route of the leg. */
export interface RouteReplacedMessage extends Envelope<'route_replaced'> {
  city: CityId
  vehicle_id: string
  dispatch_id: string
  incident_id: string
  leg: LegRef
  reroute_no: number
  reason: string
  saved_s: number
  route: LatLng[]
  corridor: CorridorEntry[]
}
export interface VehicleRemovedMessage extends Envelope<'vehicle_removed'> {
  city: CityId
  vehicle_id: string
  dispatch_id: string
  incident_id: string
  /** `back_in_service` | `failed` (`cleared` / `cancelled` from older servers). */
  reason: 'back_in_service' | 'failed' | 'cleared' | 'cancelled' | (string & {})
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
  hospital?: HospitalInfo | null
  reason?: string
}
export type PongMessage = Envelope<'pong'>

export type StreamMessage =
  | SnapshotMessage
  | TickMessage
  | VehicleAddedMessage
  | LegChangedMessage
  | RouteReplacedMessage
  | VehicleRemovedMessage
  | IncidentUpdatedMessage
  | PongMessage

export const STREAM_SCHEMA_VERSION = 1
/** WebSocket close codes used by the server. */
export const WS_CLOSE_UNKNOWN_CITY = 4404
export const WS_CLOSE_TRY_AGAIN = 1013
