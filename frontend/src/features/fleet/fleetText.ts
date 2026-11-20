import type {
  ActiveSignal,
  IncidentDetail,
  JunctionState,
  RouteChoice,
  UnitPhase,
} from '../../api/types'
import { DEST_KIND_LABEL, formatDistance } from '../../lib/format'
import type { LiveVehicle, NextJunction } from '../../store/live'

export type HeldState = Exclude<JunctionState, 'NORMAL' | 'RECOVERING'>

/** How the next junction treats this vehicle. */
export type ClaimView =
  | { kind: 'claimed'; state: HeldState; queuePos: number | null }
  | { kind: 'waiting' }
  | { kind: 'obeys' }
  | { kind: 'free_left' }

export const CLAIM_TEXT: Readonly<Record<HeldState, string>> = {
  TRANSITIONING: 'changing to green',
  PREEMPTED: 'green held',
  QUEUED: 'queued',
}

export function claimView(
  v: Pick<LiveVehicle, 'id' | 'leg'>,
  next: Pick<NextJunction, 'signal_id' | 'free_left'>,
  signals: Readonly<Record<string, ActiveSignal>>,
): ClaimView {
  if (next.free_left) return { kind: 'free_left' }
  if (v.leg && !v.leg.emergency) return { kind: 'obeys' }
  const mine = signals[next.signal_id]?.claimants.find((c) => c.vehicle_id === v.id)
  if (!mine) return { kind: 'waiting' }
  const state =
    mine.phase_state && mine.phase_state !== 'NORMAL' && mine.phase_state !== 'RECOVERING'
      ? mine.phase_state
      : mine.state === 'CONFLICT'
        ? 'QUEUED'
        : 'PREEMPTED'
  return { kind: 'claimed', state, queuePos: mine.queue_pos ?? null }
}

/** The unit status, also for older servers that only send the two-value status. */
export function phaseOf(v: Pick<LiveVehicle, 'phase' | 'status'>): UnitPhase {
  return v.phase ?? (v.status === 'ON_SCENE' ? 'ON_SCENE' : 'DISPATCHED')
}

export function destinationText(v: Pick<LiveVehicle, 'destination_info'>): string {
  const d = v.destination_info
  if (!d || d.kind === 'SCENE') return 'to the scene'
  return `to ${d.name ?? d.station_id ?? 'unnamed'} (${DEST_KIND_LABEL[d.kind]})`
}

/** "Be in lane 2 within 120 m" (or "Stay in lane 2" once past the advice point). */
export function laneByText(
  next: Pick<NextJunction, 'lane_index' | 'lanes_total' | 'lane_by_m'>,
): string {
  const lane = `lane ${next.lane_index ?? '?'}`
  if (next.lanes_total === 1) return 'Single lane: traffic pulls left'
  if (next.lane_by_m == null || next.lane_by_m <= 0) return `Stay in ${lane}`
  return `Be in ${lane} within ${formatDistance(next.lane_by_m)}`
}

/** Route choice of the leg a vehicle is driving (the RESPONSE leg also lives on the dispatch). */
export function legRouteChoice(
  d: IncidentDetail,
  dispatchId: string,
  seq: number,
): RouteChoice | null {
  const leg = d.legs?.find((l) => l.dispatch_id === dispatchId && l.seq === seq)
  if (leg?.route_choice) return leg.route_choice
  if (seq === 0) return d.dispatches.find((x) => x.id === dispatchId)?.route_choice ?? null
  return null
}
