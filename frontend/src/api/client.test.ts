import { afterEach, describe, expect, it } from 'vitest'
import { mockFetch, urlOf } from '../test/fixtures'
import { api, ApiError, buildQuery, describeError } from './client'

describe('buildQuery', () => {
  it('skips empty values and repeats arrays', () => {
    expect(buildQuery({ a: 1, b: null, c: undefined, d: '', e: ['x', 'y'], f: true })).toBe(
      '?a=1&e=x&e=y&f=true',
    )
    expect(buildQuery({})).toBe('')
  })
})

describe('api client', () => {
  afterEach(() => {
    localStorage.clear()
  })

  it('posts incidents as JSON and returns the created dispatch', async () => {
    const spy = mockFetch([
      {
        method: 'POST',
        match: /\/api\/v1\/cities\/delhi\/incidents$/,
        status: 201,
        body: { incident: { id: 'i' }, vehicle: { id: 'v' } },
      },
    ])
    const res = await api.createIncident('delhi', { type: 'POLICE', location: [28.62, 77.21] })
    expect(res.vehicle.id).toBe('v')
    const [, init] = spy.mock.calls[0]!
    expect(init?.method).toBe('POST')
    expect(JSON.parse(init?.body as string)).toEqual({ type: 'POLICE', location: [28.62, 77.21] })
    expect((init?.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect((init?.headers as Record<string, string>)['X-API-Key']).toBeUndefined()
  })

  it('sends X-API-Key on mutations only, when configured', async () => {
    localStorage.setItem('divert.apiKey', 'sekret')
    const spy = mockFetch([
      {
        method: 'DELETE',
        match: /incidents\/abc$/,
        body: { incident: { id: 'abc', status: 'CANCELLED' } },
      },
      { match: /\/api\/v1\/cities$/, body: { cities: [] } },
    ])
    await api.cancelIncident('delhi', 'abc')
    await api.cities()
    expect((spy.mock.calls[0]![1]?.headers as Record<string, string>)['X-API-Key']).toBe('sekret')
    expect((spy.mock.calls[1]![1]?.headers as Record<string, string>)['X-API-Key']).toBeUndefined()
  })

  it('builds history filters (comma lists, from/to, cursor)', async () => {
    const spy = mockFetch([{ match: /incidents\?/, body: { items: [], next_cursor: null } }])
    await api.incidents('kolkata', {
      status: ['RESOLVED', 'FAILED'],
      type: ['FIRE'],
      from: '2026-11-16T18:30:00.000Z',
      limit: 50,
      cursor: 'abc',
    })
    const url = new URL(urlOf(spy.mock.calls[0]![0]), 'http://x')
    expect(url.pathname).toBe('/api/v1/cities/kolkata/incidents')
    expect(url.searchParams.get('status')).toBe('RESOLVED,FAILED')
    expect(url.searchParams.get('type')).toBe('FIRE')
    expect(url.searchParams.get('from')).toBe('2026-11-16T18:30:00.000Z')
    expect(url.searchParams.get('limit')).toBe('50')
    expect(url.searchParams.get('cursor')).toBe('abc')
  })

  it('turns the error body into an ApiError with code, details and request id', async () => {
    mockFetch([
      {
        method: 'POST',
        match: /incidents$/,
        status: 409,
        body: {
          error: {
            code: 'no_unit_available',
            message: 'no free unit',
            details: { service: 'POLICE' },
          },
          request_id: 'req-1',
        },
      },
    ])
    const err = await api
      .createIncident('delhi', { type: 'CRIME', location: [28.6, 77.2] })
      .catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    const e = err as ApiError
    expect(e.status).toBe(409)
    expect(e.code).toBe('no_unit_available')
    expect(e.details).toEqual({ service: 'POLICE' })
    expect(e.requestId).toBe('req-1')
    expect(describeError(e)).toMatch(/No unit/)
  })

  it('maps 503 / 429 / network errors to operator messages', () => {
    expect(describeError(new ApiError(503, 'routing_unavailable', 'x'))).toMatch(
      /Nothing was dispatched/,
    )
    expect(describeError(new ApiError(429, 'rate_limited', 'x', { retry_after_s: 12.2 }))).toBe(
      'Too many requests. Retry in 13 s.',
    )
    expect(
      describeError(new ApiError(0, 'network_error', 'The DIVERT API is unreachable.')),
    ).toMatch(/unreachable/)
  })
})
