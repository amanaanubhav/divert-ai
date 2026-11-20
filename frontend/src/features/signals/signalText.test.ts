import { describe, expect, it } from 'vitest'
import type { ActiveSignal } from '../../api/types'
import {
  countdown,
  describeSignal,
  junctionState,
  orderedClaimants,
  popupHtml,
  popupModel,
  signalLabel,
  stateClass,
} from './signalText'

const SIM = '2026-11-17T06:30:00+00:00'

const base: ActiveSignal = {
  signal_id: 'J67',
  state: 'PREEMPTED',
  pos: [28.628, 77.214],
  vehicle_id: 'AMB-An2-1',
  service: 'AMBULANCE',
  lane: 'R',
  approach_bearing: 171.4,
  eta_s: 12.3,
  phase_info: {
    served_phase: 2,
    phases: 4,
    cycle_s: 120,
    geometry: 'osrm',
    transition_s_left: 0,
    expected_green_at: null,
    hold_s: 8.5,
    state_at_claim: 'red_truncate',
  },
  claimants: [
    {
      vehicle_id: 'FIRE-Fn1-1',
      service: 'FIRE_ENGINE',
      eta_s: 19,
      state: 'CONFLICT',
      phase_state: 'QUEUED',
      movement: 'STRAIGHT',
      rank: 2,
      lane: 'R',
      lane_index: 2,
      lanes_total: 2,
      phase: 0,
      queue_pos: 1,
      expected_green_at: '2026-11-17T06:30:21+00:00',
    },
    {
      vehicle_id: 'AMB-An2-1',
      service: 'AMBULANCE',
      eta_s: 12.3,
      state: 'PREEMPTED',
      phase_state: 'PREEMPTED',
      movement: 'RIGHT',
      rank: 0,
      lane: 'R',
      lane_index: 3,
      lanes_total: 3,
      phase: 2,
      queue_pos: 0,
      expected_green_at: null,
    },
  ],
}

describe('junction states', () => {
  it('maps each state to its own marker class; a legacy CONFLICT reads as queued', () => {
    expect(junctionState({ state: 'CONFLICT' })).toBe('QUEUED')
    for (const s of ['TRANSITIONING', 'PREEMPTED', 'QUEUED', 'RECOVERING'] as const)
      expect(stateClass(junctionState({ state: s }))).toBe(`is-${s.toLowerCase()}`)
  })

  it('labels every state plainly with its countdown', () => {
    expect(signalLabel({ ...base, state: 'PREEMPTED' })).toBe('12s · R')
    expect(
      signalLabel({
        ...base,
        state: 'TRANSITIONING',
        phase_info: { ...base.phase_info!, transition_s_left: 5.2 },
      }),
    ).toBe('green in 6 s')
    expect(signalLabel({ ...base, state: 'QUEUED' })).toBe('12s · R · queued (2)')
    const recovering: ActiveSignal = {
      ...base,
      state: 'RECOVERING',
      claimants: [],
      phase_info: {
        ...base.phase_info!,
        served_phase: null,
        hold_s: 0,
        recovery_phase: 1,
        recovering_until: '2026-11-17T06:30:14+00:00',
      },
    }
    expect(signalLabel(recovering, SIM)).toBe('recovering, 14 s')
    expect(signalLabel(recovering, null)).toBe('recovering')
    expect(countdown({ ...base, state: 'QUEUED' }, SIM)).toBe('held 9 s')
    expect(describeSignal(recovering, SIM)).toBe(
      'J67: recovering (released; returning to its plan), back to plan in 14 s.',
    )
  })

  it('orders the claimants served first, then the queue', () => {
    expect(orderedClaimants(base).map((c) => c.vehicle_id)).toEqual(['AMB-An2-1', 'FIRE-Fn1-1'])
    expect(describeSignal({ ...base, state: 'QUEUED' }, SIM)).toBe(
      'J67: queued (green held; another vehicle waits for its turn), held 9 s. ' +
        'AMB-An2-1 preempted, right, 12 s; FIRE-Fn1-1 queued, straight on, 19 s',
    )
  })

  it('builds the popup: countdowns, plan facts and the claimant queue with movement and rank', () => {
    const m = popupModel({ ...base, state: 'QUEUED' }, SIM, 'ITO crossing')
    expect(m.title).toBe('ITO crossing (J67)')
    expect(m.stateLabel).toBe('Queued')
    expect(m.rows).toEqual([
      { label: 'Held green', value: '9 s' },
      { label: 'At the claim', value: 'red truncated' },
      { label: 'Plan', value: '4 phases, 120 s cycle, serving phase 3' },
      { label: 'Lane', value: 'R' },
    ])
    expect(m.claimants).toEqual([
      expect.objectContaining({
        order: 1,
        vehicle: 'AMB-An2-1',
        movement: 'Right',
        rank: '0 (ambulance with patient)',
        state: 'Pre-empted',
        lane: '3 of 3',
        green: null,
      }),
      expect.objectContaining({
        order: 2,
        vehicle: 'FIRE-Fn1-1',
        movement: 'Straight on',
        rank: '2 (fire engine)',
        state: 'Queued',
        green: '21 s',
      }),
    ])
    const transitioning = popupModel(
      {
        ...base,
        state: 'TRANSITIONING',
        phase_info: { ...base.phase_info!, transition_s_left: 4 },
      },
      SIM,
    )
    expect(transitioning.rows[0]).toEqual({ label: 'Transition left', value: '4 s' })
  })

  it('escapes every value in the popup HTML', () => {
    const html = popupHtml(popupModel({ ...base, signal_id: 'J<1>' }, SIM, '<b>x</b>'))
    expect(html).not.toContain('<b>x</b>')
    expect(html).toContain('&#60;b&#62;x&#60;/b&#62;')
    expect(html).toContain('<span class="sig-key is-preempted"></span>')
    expect(html).toContain('<th>Rank</th>')
  })
})
