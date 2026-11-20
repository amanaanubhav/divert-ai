import { describe, expect, it } from 'vitest'
import { incident } from '../../test/fixtures'
import { formatHash, parseHash } from '../../store/ui'
import { isActive, laterStatus, withLiveStatus } from './merge'

describe('live status merge', () => {
  it('statuses only move forward', () => {
    expect(laterStatus('DISPATCHED', 'EN_ROUTE')).toBe('EN_ROUTE')
    expect(laterStatus('RESOLVED', 'EN_ROUTE')).toBe('RESOLVED') // stale WS message
    expect(laterStatus('ON_SCENE', 'CANCELLED')).toBe('CANCELLED')
  })

  it('applies newer live statuses to the REST list', () => {
    const list = [
      incident({ id: 'a', status: 'DISPATCHED' }),
      incident({ id: 'b', status: 'RESOLVED' }),
    ]
    const out = withLiveStatus(list, {
      a: { status: 'ON_SCENE', ts: 'x', vehicle_id: null },
      b: { status: 'EN_ROUTE', ts: 'x', vehicle_id: null },
    })
    expect(out.map((i) => i.status)).toEqual(['ON_SCENE', 'RESOLVED'])
    expect(out[1]).toBe(list[1]) // unchanged objects are reused
    expect(isActive('ON_SCENE')).toBe(true)
    expect(isActive('FAILED')).toBe(false)
  })

  it('orders the transport statuses and lets a refused cancel restore the real status', () => {
    expect(laterStatus('ON_SCENE', 'TRANSPORTING')).toBe('TRANSPORTING')
    expect(laterStatus('AT_HOSPITAL', 'TRANSPORTING')).toBe('AT_HOSPITAL')
    expect(laterStatus('AT_HOSPITAL', 'RESOLVED')).toBe('RESOLVED')
    expect(isActive('TRANSPORTING')).toBe(true)
    expect(isActive('AT_HOSPITAL')).toBe(true)
    const [out] = withLiveStatus([incident({ id: 'a', status: 'CANCELLED' })], {
      a: {
        status: 'TRANSPORTING',
        ts: 'x',
        vehicle_id: null,
        reason: 'cancel refused: patient on board',
      },
    })
    expect(out?.status).toBe('TRANSPORTING')
  })
})

describe('hash routing', () => {
  it('round-trips tab + city and rejects junk', () => {
    expect(parseHash(formatHash('history', 'kolkata'))).toEqual({ tab: 'history', city: 'kolkata' })
    expect(parseHash('#/analytics')).toEqual({ tab: 'analytics', city: null })
    expect(parseHash('#/nope/<script>')).toEqual({ tab: null, city: null })
    expect(parseHash('')).toEqual({ tab: null, city: null })
  })
})
