import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { IncidentDetail } from '../../api/types'
import { DELHI, incident, mockFetch, urlOf } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { HistoryView } from './HistoryView'

// Leaflet needs a real canvas; the replay map is covered by replay.test.ts.
vi.mock('./TrackReplay', () => ({
  TrackReplay: ({ points }: { points: unknown[] }) => <div>replay with {points.length} points</div>,
}))

const DETAIL: IncidentDetail = {
  ...incident({
    status: 'RESOLVED',
    on_scene_at: '2026-11-17T06:31:15+00:00',
    resolved_at: '2026-11-17T06:33:15+00:00',
  }),
  dispatches: [
    {
      id: 'd1',
      vehicle_id: 'PCR-Pw1-1',
      service: 'POLICE',
      station: { id: 'Pw1', name: 'Police Station Connaught Place', location: [28.63, 77.21] },
      route: [
        [28.63, 77.21],
        [28.6271, 77.2166],
      ],
      distance_m: 1090.2,
      predicted_eta_s: 73.4,
      actual_travel_s: 75,
      est_time_saved_s: 70,
      eta_model: 'kinematic-v1',
      corridor: [],
      dispatched_at: '2026-11-17T06:30:00+00:00',
      arrived_at: '2026-11-17T06:31:15+00:00',
      ended_at: '2026-11-17T06:33:15+00:00',
      end_reason: 'CLEARED',
    },
  ],
  preemptions: [
    {
      event_id: 'clm:d1:J6720393130',
      dispatch_id: 'd1',
      signal_id: 'J6720393130',
      state: 'PREEMPTED',
      lane: 'R',
      approach_bearing: 171.4,
      eta_at_claim_s: 44.6,
      claimed_at: '2026-11-17T06:30:20+00:00',
      released_at: '2026-11-17T06:30:58+00:00',
    },
  ],
  timeline: [
    {
      event_id: 'rep:1',
      dispatch_id: null,
      type: 'reported',
      ts: '2026-11-17T06:30:00+00:00',
      payload: {},
    },
    {
      event_id: 'clm:1',
      dispatch_id: 'd1',
      type: 'preemption_claimed',
      ts: '2026-11-17T06:30:20+00:00',
      payload: { signal_id: 'J6720393130', lane: 'R' },
    },
    {
      event_id: 'arr:1',
      dispatch_id: 'd1',
      type: 'arrived',
      ts: '2026-11-17T06:31:15+00:00',
      payload: { travel_s: 75 },
    },
  ],
}

describe('HistoryView', () => {
  it('pages with the keyset cursor, filters by status, and opens the detail drawer', async () => {
    const spy = mockFetch([
      {
        match: /\/api\/v1\/incidents\/[^/?]+\/track/,
        body: { incident_id: DETAIL.id, points: [{}, {}] },
      },
      { match: /\/api\/v1\/incidents\/[^/?]+$/, body: DETAIL },
      {
        match: /incidents\?.*cursor=c1/,
        body: {
          items: [incident({ id: 'second', type: 'FIRE', status: 'FAILED' })],
          next_cursor: null,
        },
      },
      {
        match: /incidents\?.*status=RESOLVED/,
        body: { items: [incident({ id: 'r1', status: 'RESOLVED' })], next_cursor: null },
      },
      {
        match: /\/api\/v1\/cities\/delhi\/incidents\?/,
        body: {
          items: [incident({ status: 'RESOLVED', on_scene_at: '2026-11-17T06:31:15+00:00' })],
          next_cursor: 'c1',
        },
      },
    ])
    renderWithProviders(<HistoryView city={DELHI} />)

    const table = await screen.findByRole('table')
    expect(within(table).getAllByRole('row')).toHaveLength(2) // header + 1
    expect(within(table).getByText('1:15')).toBeInTheDocument() // response time
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(() =>
      expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(3),
    )
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Resolved', pressed: false }))
    await waitFor(() =>
      expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(2),
    )
    const lastUrl = new URL(urlOf(spy.mock.calls.at(-1)![0]), 'http://x')
    expect(lastUrl.searchParams.get('status')).toBe('RESOLVED')
    expect(lastUrl.searchParams.get('limit')).toBe('50')

    await userEvent.click(screen.getByRole('button', { name: 'Details' }))
    const dialog = await screen.findByRole('dialog')
    await within(dialog).findByText('PCR-Pw1-1')
    expect(within(dialog).getByText('1:13')).toBeInTheDocument() // predicted
    // error = (actual - predicted) / actual, as the backend's eta_error_pct: 1.6 / 75
    expect(within(dialog).getByText('+2.1%')).toBeInTheDocument()
    expect(within(dialog).getByText('J6720393130')).toBeInTheDocument()
    expect(within(dialog).getByText('Signal pre-empted')).toBeInTheDocument()
    expect(within(dialog).getByText(/travel 1:15/)).toBeInTheDocument()
    expect(await within(dialog).findByText('replay with 2 points')).toBeInTheDocument()

    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('sends day filters as instants of local midnight in the city time zone', async () => {
    const spy = mockFetch([{ match: /incidents\?/, body: { items: [], next_cursor: null } }])
    renderWithProviders(<HistoryView city={DELHI} />)
    await screen.findByText('No incidents match these filters.')
    await userEvent.type(screen.getByLabelText('From'), '2026-11-17')
    await waitFor(() => {
      const url = new URL(urlOf(spy.mock.calls.at(-1)![0]), 'http://x')
      expect(url.searchParams.get('from')).toBe('2026-11-16T18:30:00.000Z')
    })
  })
})
