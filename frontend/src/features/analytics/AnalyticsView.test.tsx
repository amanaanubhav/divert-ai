import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { AnalyticsSummary } from '../../api/types'
import { DELHI, mockFetch, SUMMARY, urlOf } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { AnalyticsView } from './AnalyticsView'

describe('AnalyticsView', () => {
  it('renders per-city cards and the selected city charts', async () => {
    const spy = mockFetch([
      {
        match: /\/api\/v1\/cities$/,
        body: {
          cities: [
            {
              id: 'delhi',
              name: 'Delhi',
              timezone: 'Asia/Kolkata',
              center: [28.6, 77.2],
              bbox: [28.4, 76.84, 28.88, 77.35],
              zoom: 12,
            },
            {
              id: 'kolkata',
              name: 'Kolkata',
              timezone: 'Asia/Kolkata',
              center: [22.5, 88.3],
              bbox: [22.45, 88.25, 22.65, 88.5],
              zoom: 12,
            },
          ],
        },
      },
      { match: /analytics\/summary/, body: SUMMARY },
    ])
    renderWithProviders(<AnalyticsView city={DELHI} />)

    const glance = await screen.findByRole('region', { name: 'Delhi at a glance' })
    expect(within(glance).getByText('3.1%')).toBeInTheDocument() // ETA MAPE
    expect(within(glance).getByText('20.4 min')).toBeInTheDocument() // 1225 s saved

    const rt = screen.getByRole('region', { name: /Response time by type/ })
    expect(within(rt).getByText('Medical')).toBeInTheDocument()
    expect(within(rt).getByText(/5\.2 min/)).toBeInTheDocument() // p50 312 s
    expect(within(rt).getByText(/9\.0 min/)).toBeInTheDocument() // p90 540 s

    const pre = screen.getByRole('region', { name: 'Signal pre-emption' })
    expect(within(pre).getByText('41')).toBeInTheDocument()
    expect(within(pre).getByText('4.9%')).toBeInTheDocument() // 2 / 41 conflicts
    expect(within(pre).getByText('J2800811770')).toBeInTheDocument()

    expect(await screen.findByRole('button', { name: /Kolkata/ })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    const analyticsCalls = spy.mock.calls
      .map((c) => urlOf(c[0]))
      .filter((u) => u.includes('analytics'))
    const u = new URL(analyticsCalls[0]!, 'http://x')
    expect(Date.parse(u.searchParams.get('to')!) - Date.parse(u.searchParams.get('from')!)).toBe(
      7 * 24 * 3600_000,
    )
  })

  it('renders the trip-leg metrics: transport times, hospitals, leg accuracy, outcomes', async () => {
    mockFetch([
      { match: /\/api\/v1\/cities$/, body: { cities: [] } },
      { match: /analytics\/summary/, body: SUMMARY },
    ])
    renderWithProviders(<AnalyticsView city={DELHI} />)

    const transport = await screen.findByRole('region', { name: 'Patient transport · p50 / p90' })
    expect(within(transport).getByText('Transport')).toBeInTheDocument()
    expect(within(transport).getByText(/8\.0 min/)).toBeInTheDocument() // p50 480 s
    expect(within(transport).getByText('Call to hospital')).toBeInTheDocument()
    expect(within(transport).getByText(/24\.3 min/)).toBeInTheDocument() // p50 1460 s
    expect(within(transport).getByText('Handover to back in service')).toBeInTheDocument()
    expect(within(transport).getByText('AIIMS')).toBeInTheDocument()

    const legs = screen.getByRole('region', { name: 'ETA accuracy by leg' })
    expect(within(legs).getByText('Return')).toBeInTheDocument()
    expect(within(legs).getByText('1.2% · max 4%')).toBeInTheDocument()

    const outcomes = screen.getByRole('region', { name: 'Pre-emption outcomes' })
    expect(within(outcomes).getByText('Green extended').nextSibling).toHaveTextContent('9')
    expect(within(outcomes).getByText('Red truncated').nextSibling).toHaveTextContent('30')
    expect(within(outcomes).getByText('Free left passed').nextSibling).toHaveTextContent('3')
    expect(within(outcomes).getByText('Re-routes').nextSibling).toHaveTextContent('1')
    expect(within(outcomes).getByText(/23% extended/)).toBeInTheDocument()
    expect(within(outcomes).getByText('Timed out')).toBeInTheDocument()

    const dispatches = screen.getByRole('region', { name: 'Dispatches by status (whole trip)' })
    expect(within(dispatches).getByText('Completed')).toBeInTheDocument()
  })

  it('leaves the trip-leg panels out for an older server', async () => {
    // a pre-legs server: none of the trip-leg fields
    const old: AnalyticsSummary = { ...SUMMARY }
    delete old.transport_time_s
    delete old.call_to_hospital_s
    delete old.handover_to_back_in_service_s
    delete old.hospitals
    delete old.legs
    delete old.free_left_passed
    delete old.reroutes
    mockFetch([
      { match: /\/api\/v1\/cities$/, body: { cities: [] } },
      {
        match: /analytics\/summary/,
        body: {
          ...old,
          dispatches: { ...old.dispatches, by_status: undefined },
          preemptions: { total: 1, conflicts: 0, distinct_signals: 1, top_signals: [] },
        },
      },
    ])
    renderWithProviders(<AnalyticsView city={DELHI} />)
    await screen.findByRole('region', { name: 'Signal pre-emption' })
    expect(screen.queryByRole('region', { name: /Patient transport/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'ETA accuracy by leg' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Pre-emption outcomes' })).not.toBeInTheDocument()
  })
})
