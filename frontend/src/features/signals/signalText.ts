import type { ActiveSignal, Claimant, JunctionState } from '../../api/types'
import {
  JUNCTION_STATE_HINT,
  JUNCTION_STATE_LABEL,
  MOVEMENT_LABEL,
  RANK_LABEL,
  SERVICE_LABEL,
} from '../../lib/format'
import { escapeHtml } from '../map/icons'

export type ActiveJunctionState = Exclude<JunctionState, 'NORMAL'>

/** Junction state of an active signal (a pre-stage-A `CONFLICT` reads as QUEUED). */
export function junctionState(s: Pick<ActiveSignal, 'state'>): ActiveJunctionState {
  return s.state === 'CONFLICT' ? 'QUEUED' : s.state
}

/** CSS modifier of the marker: `is-transitioning`, `is-preempted`, `is-queued`, `is-recovering`. */
export function stateClass(state: ActiveJunctionState): string {
  return `is-${state.toLowerCase()}`
}

/** Simulated seconds from `simTs` to `iso` (null when either is unknown). */
export function secondsUntil(iso: string | null | undefined, simTs: string | null): number | null {
  if (!iso || !simTs) return null
  const d = (Date.parse(iso) - Date.parse(simTs)) / 1000
  return Number.isFinite(d) ? d : null
}

const secs = (s: number) => `${Math.max(0, Math.ceil(s))} s`

/** The countdown the junction shows now: transition left, hold, or recovery left. */
export function countdown(s: ActiveSignal, simTs: string | null): string | null {
  const p = s.phase_info
  if (!p) return null
  switch (junctionState(s)) {
    case 'TRANSITIONING':
      return `green in ${secs(p.transition_s_left)}`
    case 'PREEMPTED':
    case 'QUEUED':
      return `held ${secs(p.hold_s)}`
    case 'RECOVERING': {
      const left = secondsUntil(p.recovering_until, simTs)
      return left === null ? null : `back to plan in ${secs(left)}`
    }
  }
}

/** Short marker label, e.g. `12s · R`, `green in 6 s`, `12s · R · queued (2)`, `recovering`. */
export function signalLabel(s: ActiveSignal, simTs: string | null = null): string {
  const eta = `${Math.max(0, Math.round(s.eta_s))}s`
  switch (junctionState(s)) {
    case 'TRANSITIONING':
      return s.phase_info
        ? `green in ${secs(s.phase_info.transition_s_left)}`
        : `${eta} · ${s.lane}`
    case 'PREEMPTED':
      return `${eta} · ${s.lane}`
    case 'QUEUED':
      return `${eta} · ${s.lane} · queued (${s.claimants.length})`
    case 'RECOVERING': {
      const c = countdown(s, simTs)
      return c ? `recovering, ${c.replace('back to plan in ', '')}` : 'recovering'
    }
  }
}

/** Claimants in their order of service: the served one first, then the queue. */
export function orderedClaimants(s: Pick<ActiveSignal, 'claimants'>): Claimant[] {
  return [...s.claimants].sort(
    (a, b) => (a.queue_pos ?? 0) - (b.queue_pos ?? 0) || a.eta_s - b.eta_s,
  )
}

function claimantState(c: Claimant): ActiveJunctionState {
  if (c.phase_state && c.phase_state !== 'NORMAL') return c.phase_state
  return c.state === 'CONFLICT' ? 'QUEUED' : 'PREEMPTED'
}

/** Short hover text: id, state and countdown (the click popup has the rest). */
export function signalTooltip(s: ActiveSignal, simTs: string | null = null): string {
  const c = countdown(s, simTs)
  return `${s.signal_id} · ${JUNCTION_STATE_LABEL[junctionState(s)].toLowerCase()}${c ? `, ${c}` : ''} · click for the queue`
}

/** One sentence for the marker's accessible name. */
export function describeSignal(s: ActiveSignal, simTs: string | null = null): string {
  const state = junctionState(s)
  const c = countdown(s, simTs)
  const head = `${s.signal_id}: ${JUNCTION_STATE_LABEL[state].toLowerCase()} (${JUNCTION_STATE_HINT[state]})`
  if (state === 'RECOVERING') return `${head}${c ? `, ${c}` : ''}.`
  const who = orderedClaimants(s).map(
    (x) =>
      `${x.vehicle_id} ${claimantState(x).toLowerCase()}${x.movement ? `, ${MOVEMENT_LABEL[x.movement].toLowerCase()}` : ''}, ${Math.round(x.eta_s)} s`,
  )
  return `${head}${c ? `, ${c}` : ''}. ${who.join('; ')}`
}

export interface PopupRow {
  label: string
  value: string
}
export interface PopupClaimant {
  order: number
  vehicle: string
  service: string
  movement: string
  rank: string
  state: string
  eta: string
  lane: string
  green: string | null
}
export interface SignalPopup {
  title: string
  state: ActiveJunctionState
  stateLabel: string
  hint: string
  rows: PopupRow[]
  claimants: PopupClaimant[]
}

/** Everything the click popup shows: state, countdowns, plan facts and the claimant queue. */
export function popupModel(
  s: ActiveSignal,
  simTs: string | null,
  name?: string | null,
): SignalPopup {
  const state = junctionState(s)
  const p = s.phase_info
  const rows: PopupRow[] = []
  if (p) {
    if (state === 'TRANSITIONING')
      rows.push({ label: 'Transition left', value: secs(p.transition_s_left) })
    if (state === 'PREEMPTED' || state === 'QUEUED')
      rows.push({ label: 'Held green', value: secs(p.hold_s) })
    if (state === 'RECOVERING') {
      const left = secondsUntil(p.recovering_until, simTs)
      if (left !== null) rows.push({ label: 'Back to plan in', value: secs(left) })
      if (p.recovery_phase != null)
        rows.push({ label: 'Recovery green', value: `phase ${p.recovery_phase + 1}` })
    }
    if (p.state_at_claim)
      rows.push({
        label: 'At the claim',
        value: p.state_at_claim === 'green_extend' ? 'green extended' : 'red truncated',
      })
    rows.push({
      label: 'Plan',
      value: `${p.phases} phases, ${Math.round(p.cycle_s)} s cycle${p.served_phase != null ? `, serving phase ${p.served_phase + 1}` : ''}`,
    })
  }
  if (state !== 'RECOVERING') rows.push({ label: 'Lane', value: s.lane })
  const claimants = orderedClaimants(s).map((c, i) => {
    const green = secondsUntil(c.expected_green_at, simTs)
    return {
      order: (c.queue_pos ?? i) + 1,
      vehicle: c.vehicle_id,
      service: SERVICE_LABEL[c.service],
      movement: c.movement ? MOVEMENT_LABEL[c.movement] : '–',
      rank: c.rank != null ? `${c.rank} (${RANK_LABEL[c.rank] ?? 'other'})` : '–',
      state: JUNCTION_STATE_LABEL[claimantState(c)],
      eta: secs(c.eta_s),
      lane:
        c.lane_index != null && c.lanes_total != null
          ? `${c.lane_index} of ${c.lanes_total}`
          : (c.lane ?? s.lane),
      green: green !== null && green > 0 ? secs(green) : null,
    }
  })
  return {
    title: name ? `${name} (${s.signal_id})` : s.signal_id,
    state,
    stateLabel: JUNCTION_STATE_LABEL[state],
    hint: JUNCTION_STATE_HINT[state],
    rows,
    claimants,
  }
}

/** HTML of the popup (every value escaped). */
export function popupHtml(m: SignalPopup): string {
  const e = escapeHtml
  const rows = m.rows.map((r) => `<div><dt>${e(r.label)}</dt><dd>${e(r.value)}</dd></div>`).join('')
  const claimants =
    m.claimants.length === 0
      ? '<p class="sig-pop-empty">No claimants: released.</p>'
      : `<table><thead><tr><th>#</th><th>Vehicle</th><th>Movement</th><th>Rank</th><th>State</th><th>ETA</th></tr></thead><tbody>${m.claimants
          .map(
            (c) =>
              `<tr><td>${c.order}</td><td class="mono">${e(c.vehicle)}</td><td>${e(c.movement)}</td><td class="wrap">${e(c.rank)}</td><td>${e(c.state)}${c.green ? `<br><span class="sig-pop-meta">green in ${e(c.green)}</span>` : ''}</td><td>${e(c.eta)}</td></tr>`,
          )
          .join('')}</tbody></table>`
  return (
    `<div class="sig-pop">` +
    `<div class="sig-pop-title mono">${e(m.title)}</div>` +
    `<div class="sig-pop-state"><span class="sig-key ${stateClass(m.state)}"></span><strong>${e(m.stateLabel)}</strong>: ${e(m.hint)}</div>` +
    `<dl>${rows}</dl>` +
    `<div class="sig-pop-sub">Claimant queue</div>${claimants}` +
    `</div>`
  )
}
