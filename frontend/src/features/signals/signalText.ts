import type { ActiveSignal } from '../../api/types'

/** `12s · R` (ETA of the winning vehicle to the junction and its lane advisory). */
export function signalLabel(s: ActiveSignal): string {
  const eta = `${Math.max(0, Math.round(s.eta_s))}s`
  if (s.state === 'CONFLICT') return `${eta} · ${s.lane} · conflict (${s.claimants.length})`
  return `${eta} · ${s.lane}`
}

export function describeSignal(s: ActiveSignal): string {
  const who = s.claimants.map(
    (c) => `${c.vehicle_id} (${c.state.toLowerCase()}, ${Math.round(c.eta_s)} s)`,
  )
  return `${s.signal_id} ${s.state.toLowerCase()} for ${s.vehicle_id}, lane ${s.lane}. ${who.join('; ')}`
}
