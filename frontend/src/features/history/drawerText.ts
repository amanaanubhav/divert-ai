import type { IncidentDetail, LegKind, LegRecord, TimelineEvent } from '../../api/types'
import {
  formatDuration,
  formatSeconds,
  LEG_LABEL,
  MOVEMENT_LABEL,
  OUTCOME_LABEL,
  secondsBetween,
} from '../../lib/format'

/** The first leg of a kind. */
export const legOf = (legs: readonly LegRecord[], kind: LegKind) =>
  legs.find((l) => l.kind === kind)

/** Response and call-to-hospital in simulated seconds (as the analytics compute them). */
export function tripTimes(
  d: IncidentDetail,
  legs: readonly LegRecord[],
): { responseS: number | null; callToHospitalS: number | null } {
  const queue = secondsBetween(d.reported_at, d.dispatched_at) ?? 0
  const response = legOf(legs, 'RESPONSE')
  const transport = legOf(legs, 'TRANSPORT')
  const responseS =
    response?.actual_travel_s != null
      ? queue + response.actual_travel_s
      : secondsBetween(d.reported_at, d.on_scene_at)
  const callToHospitalS =
    transport?.end_reason === 'ARRIVED' && transport.ended_trip_s != null
      ? queue + transport.ended_trip_s
      : null
  return { responseS, callToHospitalS }
}

/** (actual - predicted) / actual x 100, as the backend computes `eta_error_pct`: only for
 * a leg that arrived (a cancelled, failed or superseded leg has no meaningful error). */
export function legErrorPct(
  l: Pick<LegRecord, 'predicted_eta_s' | 'actual_travel_s' | 'eta_error_pct' | 'end_reason'>,
): number | null {
  if (l.end_reason != null && l.end_reason !== 'ARRIVED') return null
  if (l.eta_error_pct != null) return l.eta_error_pct
  if (l.actual_travel_s == null || l.actual_travel_s <= 0) return null
  return (100 * (l.actual_travel_s - l.predicted_eta_s)) / l.actual_travel_s
}

/** Leg seq an event belongs to: explicit in its payload, else the leg being driven then. */
export function timelineLegs(events: readonly TimelineEvent[]): number[] {
  let current = 0
  return events.map((e) => {
    const p = e.payload ?? {}
    const explicit =
      typeof p.leg_seq === 'number'
        ? p.leg_seq
        : e.type.startsWith('leg_') && typeof p.seq === 'number'
          ? p.seq
          : null
    if (e.type === 'leg_started' && explicit !== null) current = explicit
    return explicit ?? current
  })
}

export function payloadSummary(e: TimelineEvent, legKind?: LegKind): string {
  const p = e.payload ?? {}
  const parts: string[] = []
  const str = (k: string) =>
    typeof p[k] === 'string' || typeof p[k] === 'number' ? String(p[k]) : null
  if (e.type.startsWith('leg_') && typeof p.kind === 'string' && p.kind in LEG_LABEL) {
    parts.push(`${LEG_LABEL[p.kind as LegKind].toLowerCase()} leg`)
    const dest = p.destination as { name?: unknown } | undefined
    if (dest && typeof dest.name === 'string') parts.push(`to ${dest.name}`)
    if (typeof p.predicted_eta_s === 'number' && e.type !== 'leg_ended')
      parts.push(`predicted ${formatDuration(p.predicted_eta_s)}`)
    const end = str('end_reason')
    if (end && e.type === 'leg_ended') parts.push(end.toLowerCase())
  } else if (legKind && legKind !== 'RESPONSE' && str('signal_id')) {
    parts.push(`${LEG_LABEL[legKind].toLowerCase()} leg`)
  }
  const sig = str('signal_id')
  if (sig) parts.push(sig)
  const vid = str('vehicle_id')
  if (vid && !sig && !e.type.startsWith('leg_')) parts.push(vid)
  const movement = str('movement')
  if (movement && sig && movement in MOVEMENT_LABEL)
    parts.push(MOVEMENT_LABEL[movement as keyof typeof MOVEMENT_LABEL].toLowerCase())
  const lane = str('lane')
  if (lane) parts.push(`lane ${lane}`)
  const outcome = str('outcome')
  if (outcome && outcome in OUTCOME_LABEL)
    parts.push(OUTCOME_LABEL[outcome as keyof typeof OUTCOME_LABEL].toLowerCase())
  const h = p.hospital as { name?: unknown } | undefined
  if (h && typeof h.name === 'string') parts.push(h.name)
  const reason = str('reason')
  if (reason) parts.push(reason)
  const saved = p.saved_s
  if (typeof saved === 'number') parts.push(`saves ${formatSeconds(saved)}`)
  const travel = p.travel_s
  if (typeof travel === 'number') parts.push(`travel ${formatDuration(travel)}`)
  const handover = p.handover_s
  if (typeof handover === 'number') parts.push(`handover ${formatDuration(handover)}`)
  return parts.join(' · ')
}
