import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { useToasts } from '../../components/ui/toast'
import { useUiStore } from '../../store/ui'
import { DELHI, incident, mockFetch } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { DispatchPanel } from './DispatchPanel'

const CREATED = {
  incident: incident({ type: 'MEDICAL', reported_type: 'MEDICAL' }),
  vehicle: {
    id: 'AMB-An2-1',
    dispatch_id: 'd1',
    service: 'AMBULANCE',
    status: 'DISPATCHED',
    station: { id: 'An2', name: 'Far Hospital', location: [28.7, 77.3], straight_line_m: 12000 },
    route: [
      [28.7, 77.3],
      [28.62, 77.21],
    ],
    distance_m: 14_100,
    eta_s: 812,
    eta_model: 'kinematic-v1',
    corridor: [],
  },
}

describe('DispatchPanel', () => {
  beforeEach(() => {
    useUiStore.setState({ pending: null, dispatchType: 'MEDICAL', selectedVehicle: null })
    useToasts.setState({ toasts: [] })
  })

  it('asks for a map click until a location is picked', () => {
    renderWithProviders(<DispatchPanel city={DELHI} />)
    expect(screen.getByText(/Click the map inside the dashed/)).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /Medical/ })).toHaveAttribute('aria-checked', 'true')
  })

  it('previews the nearest station of the service with a free unit (haversine)', () => {
    useUiStore.setState({ pending: [28.62, 77.21] })
    renderWithProviders(<DispatchPanel city={DELHI} />)
    // "Near Hospital (full)" is closer but has no free unit
    expect(screen.getByText('Far Hospital')).toBeInTheDocument()
    expect(screen.getByText(/km straight-line/)).toBeInTheDocument()
  })

  it('switching the type re-targets the preview', async () => {
    useUiStore.setState({ pending: [28.62, 77.21] })
    renderWithProviders(<DispatchPanel city={DELHI} />)
    await userEvent.click(screen.getByRole('radio', { name: /Crime/ }))
    expect(screen.getByText('Police Station Connaught Place')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Dispatch police' })).toBeInTheDocument()
  })

  it('dispatches and selects the vehicle on 201', async () => {
    useUiStore.setState({ pending: [28.62, 77.21] })
    const spy = mockFetch([
      { method: 'POST', match: /\/api\/v1\/cities\/delhi\/incidents$/, status: 201, body: CREATED },
    ])
    renderWithProviders(<DispatchPanel city={DELHI} />)
    await userEvent.type(screen.getByLabelText(/Notes/), 'two injured')
    await userEvent.click(screen.getByRole('button', { name: 'Dispatch ambulance' }))
    await screen.findByText('AMB-An2-1 dispatched')
    expect(JSON.parse(spy.mock.calls[0]![1]?.body as string)).toEqual({
      type: 'MEDICAL',
      location: [28.62, 77.21],
      description: 'two injured',
    })
    expect(useUiStore.getState().pending).toBeNull()
    expect(useUiStore.getState().selectedVehicle).toBe('AMB-An2-1')
  })

  it.each([
    [409, 'no_unit_available', 'No unit available', /No unit of that service is free/],
    [503, 'routing_unavailable', 'Routing unavailable', /Nothing was dispatched/],
    [422, 'invalid_incident', 'Invalid incident', /outside the city/],
  ])('shows a toast for %i', async (status, code, title, message) => {
    useUiStore.setState({ pending: [28.62, 77.21] })
    mockFetch([
      {
        method: 'POST',
        match: /incidents$/,
        status,
        body: { error: { code, message: 'location is outside the city' }, request_id: 'rid-9' },
      },
    ])
    renderWithProviders(<DispatchPanel city={DELHI} />)
    await userEvent.click(screen.getByRole('button', { name: 'Dispatch ambulance' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(title)
    expect(alert).toHaveTextContent(message)
    expect(alert).toHaveTextContent('request rid-9')
    expect(useUiStore.getState().pending).toEqual([28.62, 77.21]) // kept for a retry
  })

  it('refuses a location outside the city bbox without calling the API', async () => {
    useUiStore.setState({ pending: [22.57, 88.36] }) // Kolkata
    const spy = mockFetch([])
    renderWithProviders(<DispatchPanel city={DELHI} />)
    await userEvent.click(screen.getByRole('button', { name: 'Dispatch ambulance' }))
    await waitFor(() => expect(useToasts.getState().toasts[0]?.title).toBe('Outside service area'))
    expect(spy).not.toHaveBeenCalled()
  })
})
