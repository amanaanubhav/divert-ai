import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { useLiveStore } from '../../store/live'
import { DELHI, incident, mockFetch, urlOf } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { IncidentsPanel } from './IncidentsPanel'
import { CANCEL_BLOCKED_HINT, canCancel } from './merge'

describe('IncidentsPanel', () => {
  beforeEach(() => useLiveStore.getState().reset('delhi'))

  it('shows the transport statuses with the hospital and blocks cancel with a tooltip', async () => {
    renderWithProviders(
      <IncidentsPanel
        city={DELHI}
        isLoading={false}
        incidents={[
          incident({
            id: 't1',
            type: 'MEDICAL',
            status: 'TRANSPORTING',
            hospital_station_id: 'An2',
          }),
          incident({ id: 'h1', type: 'MEDICAL', status: 'AT_HOSPITAL', hospital_station_id: 'Hx' }),
        ]}
      />,
    )
    expect(screen.getByText('Transporting')).toBeInTheDocument()
    expect(screen.getByText('At hospital')).toBeInTheDocument()
    expect(screen.getByText('to Far Hospital')).toBeInTheDocument() // station name lookup
    expect(screen.getByText('to Hx')).toBeInTheDocument() // unknown id shown as is
    expect(screen.getByRole('heading', { name: 'Incidents · 2 active' })).toBeInTheDocument()

    const blocked = screen.getAllByRole('button', { name: /not possible: patient on board/ })
    expect(blocked).toHaveLength(2)
    expect(blocked[0]).toHaveAttribute('aria-disabled', 'true')
    expect(blocked[0]).toHaveAttribute('title', CANCEL_BLOCKED_HINT)
    expect(blocked[0]).toHaveAccessibleDescription(CANCEL_BLOCKED_HINT)
    await userEvent.click(blocked[0]!)
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
    expect(await screen.findByText('Cannot cancel')).toBeInTheDocument()
  })

  it('handles a 409 cannot_cancel_transport (the patient was picked up meanwhile)', async () => {
    const spy = mockFetch([
      {
        method: 'DELETE',
        match: /\/incidents\/r1$/,
        status: 409,
        body: {
          error: {
            code: 'cannot_cancel_transport',
            message: 'patient on board',
            details: { status: 'TRANSPORTING' },
          },
          request_id: 'req-9',
        },
      },
    ])
    renderWithProviders(
      <IncidentsPanel
        city={DELHI}
        isLoading={false}
        incidents={[incident({ id: 'r1', type: 'MEDICAL', status: 'ON_SCENE' })]}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: 'Cancel medical incident' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('Cannot cancel')).toBeInTheDocument()
    expect(within(alert).getByText(/A patient is on board/)).toBeInTheDocument()
    expect(within(alert).getByText('request req-9')).toBeInTheDocument()
    expect(urlOf(spy.mock.calls[0]?.[0])).toMatch(/\/api\/v1\/cities\/delhi\/incidents\/r1$/)
  })

  it('knows which statuses can still be cancelled', () => {
    expect(canCancel('EN_ROUTE')).toBe(true)
    expect(canCancel('ON_SCENE')).toBe(true)
    expect(canCancel('TRANSPORTING')).toBe(false)
    expect(canCancel('AT_HOSPITAL')).toBe(false)
    expect(canCancel('RESOLVED')).toBe(false)
  })
})
