import { describe, expect, it } from 'vitest'
import { LEG_COLOR, SERVICE_COLOR } from '../../lib/theme'
import type { LiveVehicle } from '../../store/live'
import { vehicleDoc } from '../../test/fixtures'
import { destinationHospitals, freeLeftAhead, legLineStyles } from './legData'

const motion = {
  fromAlong: 0,
  toAlong: 0,
  fromPos: [0, 0],
  toPos: [0, 0],
  t0: 0,
  durMs: 0,
} as const
const live = (over: Partial<LiveVehicle>): LiveVehicle => ({ ...vehicleDoc(), motion, ...over })
const HOSP = {
  id: 'Hn9',
  name: 'City Hospital',
  location: [28.62, 77.21] as const,
  emergency: 'yes',
}

describe('leg layers', () => {
  it('highlights the hospital of transports and handovers only', () => {
    const out = destinationHospitals([
      live({ id: 'A', phase: 'TRANSPORTING', hospital: HOSP }),
      live({ id: 'B', phase: 'AT_HOSPITAL', hospital: HOSP }),
      live({ id: 'C', phase: 'RETURNING', hospital: HOSP }), // kept after transport: not highlighted
      live({ id: 'D', phase: 'DISPATCHED' }),
    ])
    expect([...out.keys()]).toEqual(['Hn9'])
    expect(out.get('Hn9')?.vehicles).toEqual(['A', 'B'])
  })

  it('tags free-left junctions still ahead of each vehicle', () => {
    const corridor = [
      { ...vehicleDoc().corridor![0]!, signal_id: 'J1', along_m: 100, free_left: true },
      { ...vehicleDoc().corridor![0]!, signal_id: 'J2', along_m: 900, free_left: true },
      { ...vehicleDoc().corridor![0]!, signal_id: 'J3', along_m: 950, free_left: false },
    ]
    const out = freeLeftAhead([live({ id: 'A', along_m: 500, corridor })])
    expect([...out.keys()]).toEqual(['A|J2|0'])
  })

  it('draws RESPONSE solid in the service colour, TRANSPORT solid violet, RETURN dashed', () => {
    const response = legLineStyles('RESPONSE', SERVICE_COLOR.AMBULANCE)
    expect(response.remaining).toMatchObject({ color: SERVICE_COLOR.AMBULANCE })
    expect(response.remaining).not.toHaveProperty('dashArray')
    const transport = legLineStyles('TRANSPORT', LEG_COLOR.TRANSPORT)
    expect(transport.remaining).toMatchObject({ color: '#6c35c2' })
    expect(transport.remaining).not.toHaveProperty('dashArray')
    expect(legLineStyles('RETURN', LEG_COLOR.RETURN).remaining).toMatchObject({
      color: '#6e7781',
      dashArray: '8 7',
    })
  })
})
