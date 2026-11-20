import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { useLiveStore } from '../../store/live'
import { useUiStore } from '../../store/ui'
import { DELHI, vehicleDoc } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { FleetPanel } from './FleetPanel'

describe('FleetPanel', () => {
  beforeEach(() => {
    useLiveStore.getState().reset('delhi')
    useUiStore.setState({ selectedVehicle: null, focus: null })
  })

  it('shows an empty state', () => {
    renderWithProviders(<FleetPanel city={DELHI} />)
    expect(screen.getByText(/No vehicles on the road/)).toBeInTheDocument()
  })

  it('lists callsign, station, ETA, next signal with claim state and est. time saved', async () => {
    useLiveStore.getState().apply(
      {
        type: 'snapshot',
        v: 1,
        city: 'delhi',
        tick: 1,
        sim_ts: null,
        vehicles: [vehicleDoc({ along_m: 1000, eta_s: 120, speed_mps: 12.5, preempted_passed: 2 })],
        signals: [
          {
            signal_id: 'J10',
            state: 'PREEMPTED',
            pos: [28.61, 77.2],
            vehicle_id: 'PCR-Pw1-1',
            service: 'POLICE',
            lane: 'L',
            approach_bearing: 0,
            eta_s: 9,
            claimants: [
              { vehicle_id: 'PCR-Pw1-1', service: 'POLICE', eta_s: 9, state: 'PREEMPTED' },
            ],
          },
        ],
      },
      0,
    )
    renderWithProviders(<FleetPanel city={DELHI} />)
    expect(screen.getByText('PCR-Pw1-1')).toBeInTheDocument()
    expect(screen.getByText('Police Station Connaught Place')).toBeInTheDocument()
    expect(screen.getByLabelText('ETA 2:00')).toBeInTheDocument()
    expect(screen.getByText(/next ITO crossing in 112 m · lane L/)).toBeInTheDocument()
    expect(screen.getByText('preempted')).toBeInTheDocument()
    expect(screen.getByText('70 s')).toBeInTheDocument() // 2 junctions x 35 s city delay
    expect(screen.getByText('45 km/h')).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: 'Route progress' })).toHaveAttribute(
      'aria-valuenow',
      '48',
    )

    await userEvent.click(screen.getByRole('button', { pressed: false }))
    expect(useUiStore.getState().selectedVehicle).toBe('PCR-Pw1-1')
    expect(useUiStore.getState().focus?.pos).toEqual([28.6, 77.2])
  })
})
