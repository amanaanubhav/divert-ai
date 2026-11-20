import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { RoutePreview } from '../../store/ui'
import { medicalDetail, mockFetch } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { legErrorPct, payloadSummary, timelineLegs, tripTimes } from './drawerText'
import { IncidentDrawer } from './IncidentDrawer'
import { drawableLegs } from './replay'

// Leaflet needs a real canvas; the replay logic is covered by replay.test.ts.
vi.mock('./TrackReplay', () => ({
  TrackReplay: ({ points, preview }: { points: unknown[]; preview: RoutePreview[] | null }) => (
    <div>
      replay with {points.length} points, {preview?.length ?? 0} alternatives drawn
    </div>
  ),
}))

describe('IncidentDrawer with trip legs', () => {
  it('shows every leg with predicted vs actual, the hospital, the explainer and the timeline', async () => {
    const d = medicalDetail()
    mockFetch([
      {
        match: /\/api\/v1\/incidents\/m1\/track/,
        body: { incident_id: 'm1', points: [{}, {}, {}] },
      },
      { match: /\/api\/v1\/incidents\/m1$/, body: d },
    ])
    renderWithProviders(
      <IncidentDrawer incidentId="m1" timeZone="Asia/Kolkata" onClose={vi.fn()} />,
    )
    const dialog = await screen.findByRole('dialog')
    await within(dialog).findByText('Trip legs · 3')

    // summary: transport times; response and call-to-hospital in simulated seconds
    expect(within(dialog).getByText('Transport started')).toBeInTheDocument()
    expect(within(dialog).getByText('At hospital')).toBeInTheDocument()
    expect(within(dialog).getByText('Call to hospital')).toBeInTheDocument()
    expect(within(dialog).getByText('14:55')).toBeInTheDocument() // 0 s queue + 895 s trip
    expect(within(dialog).getByText('Completed')).toBeInTheDocument() // trip status

    const transport = within(dialog).getByRole('article', { name: 'Transport leg' })
    expect(within(transport).getByText('to City Hospital (hospital)')).toBeInTheDocument()
    expect(within(transport).getByText('4:26')).toBeInTheDocument() // predicted 266.3 s
    expect(within(transport).getByText('4:30')).toBeInTheDocument() // actual 270 s
    expect(within(transport).getByText('+1.4%')).toBeInTheDocument()
    expect(within(transport).getByText(/1 free left/)).toBeInTheDocument()
    expect(within(transport).getByText(/1 pre-empted/)).toBeInTheDocument()
    const ret = within(dialog).getByRole('article', { name: 'Return leg' })
    expect(within(ret).getByText(/obeys signals/)).toBeInTheDocument()
    expect(within(ret).getByText('0.0%')).toBeInTheDocument()

    // hospital and why it was chosen
    const hosp = within(dialog).getByRole('region', { name: 'Hospital' })
    expect(within(hosp).getAllByText('City Hospital')[0]).toHaveClass('font-medium')
    expect(
      within(hosp).getByText(/confirmed emergency department, 0.2 min more/),
    ).toBeInTheDocument()
    expect(within(hosp).getByText('Hospitals considered · 2')).toBeInTheDocument()

    // route choice of the RESPONSE leg: hovering previews the alternative on the replay map
    const response = within(dialog).getByRole('article', { name: 'Response leg' })
    const why = within(response).getByText('Why this route')
    await userEvent.hover(why)
    expect(within(dialog).getByText(/1 alternatives drawn/)).toBeInTheDocument()
    await userEvent.unhover(why)
    expect(within(dialog).getByText(/0 alternatives drawn/)).toBeInTheDocument()

    // pre-emptions with leg, movement, lane and outcome
    const pre = within(dialog).getByRole('region', { name: /Signal pre-emptions/ })
    expect(within(pre).getByText('0 green extended · 1 red truncated')).toBeInTheDocument()
    const row = within(pre).getByText('J30').closest('tr')!
    expect(within(row).getByText('Transport')).toBeInTheDocument()
    expect(within(row).getByText('Right')).toBeInTheDocument()
    expect(within(row).getByText('2 of 2')).toBeInTheDocument()
    expect(within(row).getByText('Cleared')).toBeInTheDocument()

    // timeline grouped by leg
    const tl = within(dialog).getByRole('region', { name: 'Timeline' })
    expect(within(tl).getByText('Transport leg')).toBeInTheDocument()
    expect(within(tl).getByText('Return leg')).toBeInTheDocument()
    expect(within(tl).getByText('Patient handed over')).toBeInTheDocument()
    expect(within(tl).getByText('handover 10:00')).toBeInTheDocument()
  })
})

describe('drawer helpers', () => {
  it('computes per-leg errors like the backend and trip times in simulated seconds', () => {
    expect(
      legErrorPct({
        predicted_eta_s: 73.4,
        actual_travel_s: 75,
        eta_error_pct: null,
        end_reason: null,
      }),
    ).toBeCloseTo(2.13, 2)
    expect(
      legErrorPct({
        predicted_eta_s: 1,
        actual_travel_s: null,
        eta_error_pct: null,
        end_reason: null,
      }),
    ).toBeNull()
    // a cancelled leg stopped short: no error (the drawer shows "–"), never a recomputed -202 %
    for (const end_reason of ['CANCELLED', 'FAILED', 'SUPERSEDED'] as const)
      expect(
        legErrorPct({
          predicted_eta_s: 145,
          actual_travel_s: 47.9,
          eta_error_pct: null,
          end_reason,
        }),
      ).toBeNull()
    expect(
      legErrorPct({
        predicted_eta_s: 145,
        actual_travel_s: 47.9,
        eta_error_pct: -202.7,
        end_reason: 'ARRIVED',
      }),
    ).toBe(-202.7)
    const d = medicalDetail()
    expect(tripTimes(d, drawableLegs(d))).toEqual({ responseS: 152, callToHospitalS: 895 })
  })

  it('assigns timeline events to legs', () => {
    expect(timelineLegs(medicalDetail().timeline)).toEqual([0, 0, 0, 1, 1, 1, 1, 1, 1, 2, 2])
    expect(
      payloadSummary({
        event_id: 'x',
        dispatch_id: null,
        type: 'leg_ended',
        ts: '',
        payload: { kind: 'TRANSPORT', end_reason: 'ARRIVED', travel_s: 270 },
      }),
    ).toBe('transport leg · arrived · travel 4:30')
  })

  it('falls back to the dispatch route as the RESPONSE leg for pre-legs payloads', () => {
    const d = medicalDetail()
    delete d.legs
    const legs = drawableLegs(d)
    expect(legs).toHaveLength(1)
    expect(legs[0]).toMatchObject({ seq: 0, kind: 'RESPONSE', dispatch_id: 'd9' })
  })
})
