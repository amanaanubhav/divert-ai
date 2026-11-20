import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
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
})
