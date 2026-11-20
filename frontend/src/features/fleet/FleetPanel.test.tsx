import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import type { ActiveSignal, IncidentDetail, LiveVehicleDoc } from '../../api/types'
import { useLiveStore } from '../../store/live'
import { useUiStore } from '../../store/ui'
import { DELHI, incident, mockFetch, ROUTE, vehicleDoc } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { FleetPanel } from './FleetPanel'
import { laneByText } from './fleetText'

function seed(
  vehicles: LiveVehicleDoc[],
  signals: ActiveSignal[] = [],
  simTs: string | null = null,
) {
  useLiveStore
    .getState()
    .apply({ type: 'snapshot', v: 1, city: 'delhi', tick: 1, sim_ts: simTs, vehicles, signals }, 0)
}

const CLAIMED_J10: ActiveSignal = {
  signal_id: 'J10',
  state: 'PREEMPTED',
  pos: [28.61, 77.2],
  vehicle_id: 'PCR-Pw1-1',
  service: 'POLICE',
  lane: 'L',
  approach_bearing: 0,
  eta_s: 9,
  claimants: [{ vehicle_id: 'PCR-Pw1-1', service: 'POLICE', eta_s: 9, state: 'PREEMPTED' }],
}

const TRANSPORTING = vehicleDoc({
  id: 'AMB-An2-1',
  service: 'AMBULANCE',
  station_id: 'An2',
  incident_type: 'MEDICAL',
  phase: 'TRANSPORTING',
  leg: { seq: 1, kind: 'TRANSPORT', emergency: true },
  destination_info: {
    kind: 'HOSPITAL',
    station_id: 'Hn9',
    name: 'City Hospital',
    location: [28.61, 77.21],
  },
  along_m: 400,
  eta_s: 95,
  speed_mps: 8,
  next_movement: {
    signal_id: 'J10',
    pass_no: 0,
    movement: 'RIGHT',
    lane_index: 3,
    lanes_total: 3,
    side: 'R',
    text: 'Right lane (3 of 3): protected right turn',
    advise_at_m: 520,
    distance_m: 711.95,
    free_left: false,
  },
  speed_advisory_mps: 5,
})

describe('FleetPanel', () => {
  beforeEach(() => {
    useLiveStore.getState().reset('delhi')
    useUiStore.setState({ selectedVehicle: null, focus: null, routePreview: null })
  })

  it('shows an empty state', () => {
    renderWithProviders(<FleetPanel city={DELHI} />)
    expect(screen.getByText(/No vehicles on the road/)).toBeInTheDocument()
  })

  it('lists callsign, station, ETA, next signal with claim state and est. time saved (older server)', async () => {
    mockFetch([])
    seed(
      [vehicleDoc({ along_m: 1000, eta_s: 120, speed_mps: 12.5, preempted_passed: 2 })],
      [CLAIMED_J10],
    )
    renderWithProviders(<FleetPanel city={DELHI} />)
    expect(screen.getByText('PCR-Pw1-1')).toBeInTheDocument()
    expect(screen.getByText('Unit of Police Station Connaught Place')).toBeInTheDocument()
    expect(screen.getByText('Responding')).toBeInTheDocument() // phase from the legacy status
    expect(screen.getByLabelText('ETA 2:00')).toBeInTheDocument()
    expect(screen.getByText('ITO crossing')).toBeInTheDocument()
    expect(screen.getByText('Next signal')).toBeInTheDocument()
    expect(screen.getByText('green held')).toBeInTheDocument()
    expect(screen.getByText('lane L')).toBeInTheDocument()
    expect(screen.getByText('70 s')).toBeInTheDocument() // 2 junctions x 35 s city delay
    expect(screen.getByText('45 km/h')).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: 'Leg progress' })).toHaveAttribute(
      'aria-valuenow',
      '48',
    )

    await userEvent.click(screen.getByRole('button', { pressed: false }))
    expect(useUiStore.getState().selectedVehicle).toBe('PCR-Pw1-1')
    expect(useUiStore.getState().focus?.pos).toEqual([28.6, 77.2])
  })

  it('shows the phase, leg and destination, next movement, lane diagram and speed advisory', () => {
    seed(
      [TRANSPORTING],
      [
        {
          ...CLAIMED_J10,
          state: 'QUEUED',
          vehicle_id: 'FIRE-Fn1-1',
          claimants: [
            {
              vehicle_id: 'AMB-An2-1',
              service: 'AMBULANCE',
              eta_s: 40,
              state: 'CONFLICT',
              phase_state: 'QUEUED',
              movement: 'RIGHT',
              rank: 0,
              queue_pos: 1,
            },
          ],
        },
      ],
    )
    renderWithProviders(<FleetPanel city={DELHI} />)
    expect(screen.getByText('Transporting')).toBeInTheDocument()
    expect(screen.getByText('Transport')).toBeInTheDocument()
    expect(screen.getByText('to City Hospital (hospital)')).toBeInTheDocument()
    expect(screen.getByText('Right')).toBeInTheDocument()
    expect(screen.getByText('queued (position 1)')).toBeInTheDocument()
    expect(screen.getByText('Right lane (3 of 3): protected right turn')).toBeInTheDocument()
    expect(
      screen.getByRole('img', { name: 'Lane 3 of 3, counted from the left kerb' }),
    ).toBeVisible()
    expect(screen.getByText('Be in lane 3 within 120 m')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(
      'Slow to 18 km/h: green expected on arrival.',
    )
  })

  it('shows planning and dwell states of stopped units, and RETURN legs obey signals', () => {
    seed(
      [
        vehicleDoc({
          id: 'AMB-An3-1',
          service: 'AMBULANCE',
          status: 'ON_SCENE',
          phase: 'ON_SCENE',
          planning: true,
          plan_error: 'routing timeout',
          along_m: 2088.13,
        }),
        vehicleDoc({
          id: 'AMB-An2-1',
          service: 'AMBULANCE',
          status: 'ON_SCENE',
          phase: 'AT_HOSPITAL',
          leg: { seq: 1, kind: 'TRANSPORT', emergency: true },
          along_m: 2088.13,
          dwell_s: 200,
          dwell_target_s: 600,
        }),
        vehicleDoc({
          id: 'PCR-Pw1-1',
          phase: 'RETURNING',
          leg: { seq: 1, kind: 'RETURN', emergency: false },
          destination_info: {
            kind: 'STATION',
            station_id: 'Pw1',
            name: 'Police Station Connaught Place',
            location: [28.63, 77.21],
          },
          along_m: 1000,
        }),
      ],
      [],
      '2026-11-17T07:00:00+00:00',
    )
    renderWithProviders(<FleetPanel city={DELHI} />)
    expect(screen.getByText('Planning route…')).toBeInTheDocument()
    expect(screen.getByText('retrying: routing timeout')).toBeInTheDocument()
    expect(screen.getByText('At hospital')).toBeInTheDocument()
    expect(screen.getByText('Patient handover')).toBeInTheDocument()
    expect(screen.getByText('3:20')).toBeInTheDocument()
    expect(screen.getByText(/of 10:00/)).toBeInTheDocument()
    expect(screen.getByText('Returning')).toBeInTheDocument()
    expect(screen.getByText('to Police Station Connaught Place (station)')).toBeInTheDocument()
    expect(screen.getByText('obeys the signal (no pre-emption)')).toBeInTheDocument()
  })

  it('explains the route of the selected vehicle and previews the alternatives on hover', async () => {
    const detail: IncidentDetail = {
      ...incident({ id: 'i1', type: 'MEDICAL', status: 'TRANSPORTING' }),
      dispatches: [],
      preemptions: [],
      timeline: [],
      legs: [
        {
          dispatch_id: 'd1',
          seq: 1,
          kind: 'TRANSPORT',
          emergency: true,
          origin: [28.6, 77.2],
          destination: {
            kind: 'HOSPITAL',
            station_id: 'Hn9',
            name: 'City Hospital',
            location: [28.61, 77.21],
          },
          route: ROUTE,
          distance_m: 2088,
          predicted_eta_s: 180,
          actual_travel_s: null,
          eta_error_pct: null,
          eta_model: 'kinematic-v3',
          planned_at: null,
          started_at: null,
          ended_at: null,
          end_reason: null,
          reroutes: 0,
          started_trip_s: 600,
          ended_trip_s: null,
          corridor: [],
          hospital_choice: null,
          route_choice: {
            chosen: 'A',
            reason: 'Route A: 1.2 min faster than B (B: Ring Road congested)',
            tie_break: null,
            candidates: [
              {
                label: 'A',
                source: 'osrm',
                distance_m: 2088,
                base_time_s: 150,
                congested_time_s: 180,
                junction_delay_s: 6,
                penalties_s: 0,
                cost_s: 186,
                crowd_index: 0.31,
                signals: 1,
                right_turns: 1,
                contention_junctions: [],
                incident_zones: [],
                congested_road: 'Janpath',
                route: ROUTE,
              },
              {
                label: 'B',
                source: 'osrm',
                distance_m: 2300,
                base_time_s: 170,
                congested_time_s: 250,
                junction_delay_s: 8,
                penalties_s: 0,
                cost_s: 258,
                crowd_index: 0.55,
                signals: 3,
                right_turns: 2,
                contention_junctions: [],
                incident_zones: [],
                congested_road: 'Ring Road',
                route: [
                  [28.6, 77.2],
                  [28.6, 77.21],
                  [28.61, 77.21],
                ],
              },
            ],
          },
        },
      ],
    }
    mockFetch([{ match: /\/api\/v1\/incidents\/i1$/, body: detail }])
    seed([TRANSPORTING])
    useUiStore.setState({ selectedVehicle: 'AMB-An2-1' })
    renderWithProviders(<FleetPanel city={DELHI} />)

    const why = await screen.findByText('Why this route')
    expect(screen.getByText(/1 alternative/)).toBeInTheDocument()
    await userEvent.hover(why)
    const preview = useUiStore.getState().routePreview
    expect(preview?.map((p) => p.label)).toEqual(['B'])
    await userEvent.click(why)
    const list = screen.getByRole('list', { name: 'Route candidates' })
    expect(within(list).getByText(/Ring Road congested|busiest: Ring Road/)).toBeInTheDocument()
    await userEvent.hover(within(list).getByText('Route B'))
    expect(useUiStore.getState().routePreview?.[0]?.emphasis).toBe(true)
    await userEvent.unhover(why)
    expect(useUiStore.getState().routePreview).toBeNull()
  })
})

describe('laneByText', () => {
  it('says where to be in lane, or to stay', () => {
    expect(laneByText({ lane_index: 2, lanes_total: 3, lane_by_m: 120 })).toBe(
      'Be in lane 2 within 120 m',
    )
    expect(laneByText({ lane_index: 2, lanes_total: 3, lane_by_m: -5 })).toBe('Stay in lane 2')
    expect(laneByText({ lane_index: 1, lanes_total: 1, lane_by_m: 50 })).toBe(
      'Single lane: traffic pulls left',
    )
  })
})
