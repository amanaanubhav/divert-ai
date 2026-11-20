import { apiUrl, getApiKey } from '../config'
import type {
  ActiveSignalsResponse,
  AnalyticsSummary,
  ApiErrorBody,
  CitiesResponse,
  CityDetail,
  IncidentCancelled,
  IncidentCreate,
  IncidentCreated,
  IncidentDetail,
  IncidentFilters,
  IncidentPage,
  ReadyResponse,
  TrackResponse,
  VehiclesResponse,
} from './types'

/** Any non-2xx answer (or a network failure: `status` 0, `code` `network_error`). */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: Record<string, unknown>
  readonly requestId: string | null

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
    requestId: string | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
    this.requestId = requestId
  }
}

function isErrorBody(x: unknown): x is ApiErrorBody {
  if (typeof x !== 'object' || x === null || !('error' in x)) return false
  const e = x.error
  return typeof e === 'object' && e !== null && 'code' in e && 'message' in e
}

type Query = Record<string, string | number | boolean | readonly string[] | null | undefined>

export function buildQuery(q: Query): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(q)) {
    if (v == null || v === '') continue
    if (Array.isArray(v)) {
      for (const item of v as readonly string[]) sp.append(k, item)
    } else {
      sp.append(k, String(v))
    }
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'DELETE'
  body?: unknown
  signal?: AbortSignal | undefined
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = opts.method ?? 'GET'
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json'
  if (method !== 'GET') {
    const key = getApiKey()
    if (key) headers['X-API-Key'] = key
  }
  let res: Response
  try {
    res = await fetch(apiUrl(path), {
      method,
      headers,
      body: opts.body === undefined ? null : JSON.stringify(opts.body),
      signal: opts.signal ?? null,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiError(0, 'network_error', 'The DIVERT API is unreachable. Check your connection.')
  }
  const requestId = res.headers.get('X-Request-ID')
  const text = await res.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = null
    }
  }
  if (!res.ok) {
    if (isErrorBody(data)) {
      throw new ApiError(
        res.status,
        data.error.code,
        data.error.message,
        data.error.details ?? {},
        data.request_id ?? requestId,
      )
    }
    throw new ApiError(
      res.status,
      `http_${res.status}`,
      res.statusText || 'Request failed',
      {},
      requestId,
    )
  }
  return data as T
}

const v1 = (p: string) => `/api/v1${p}`
const enc = encodeURIComponent

export const api = {
  ready: (signal?: AbortSignal) => request<ReadyResponse>('/readyz', { signal }),
  cities: (signal?: AbortSignal) => request<CitiesResponse>(v1('/cities'), { signal }),
  city: (city: string, signal?: AbortSignal) =>
    request<CityDetail>(v1(`/cities/${enc(city)}`), { signal }),
  createIncident: (city: string, body: IncidentCreate) =>
    request<IncidentCreated>(v1(`/cities/${enc(city)}/incidents`), { method: 'POST', body }),
  cancelIncident: (city: string, incidentId: string) =>
    request<IncidentCancelled>(v1(`/cities/${enc(city)}/incidents/${enc(incidentId)}`), {
      method: 'DELETE',
    }),
  incidents: (city: string, f: IncidentFilters = {}, signal?: AbortSignal) =>
    request<IncidentPage>(
      v1(`/cities/${enc(city)}/incidents`) +
        buildQuery({
          status: f.status && f.status.length > 0 ? f.status.join(',') : undefined,
          type: f.type && f.type.length > 0 ? f.type.join(',') : undefined,
          from: f.from,
          to: f.to,
          limit: f.limit,
          cursor: f.cursor,
        }),
      { signal },
    ),
  incident: (id: string, signal?: AbortSignal) =>
    request<IncidentDetail>(v1(`/incidents/${enc(id)}`), { signal }),
  track: (id: string, maxPoints = 1000, signal?: AbortSignal) =>
    request<TrackResponse>(
      v1(`/incidents/${enc(id)}/track`) + buildQuery({ max_points: maxPoints }),
      {
        signal,
      },
    ),
  vehicles: (city: string, includeRoute = true, signal?: AbortSignal) =>
    request<VehiclesResponse>(
      v1(`/cities/${enc(city)}/vehicles`) + buildQuery({ include_route: includeRoute }),
      { signal },
    ),
  activeSignals: (city: string, signal?: AbortSignal) =>
    request<ActiveSignalsResponse>(v1(`/cities/${enc(city)}/signals/active`), { signal }),
  analytics: (city: string, range: { from?: string; to?: string } = {}, signal?: AbortSignal) =>
    request<AnalyticsSummary>(
      v1(`/cities/${enc(city)}/analytics/summary`) + buildQuery({ from: range.from, to: range.to }),
      { signal },
    ),
}

/** A one-line, operator-friendly message for an error (used by toasts). */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.code) {
      case 'no_unit_available':
        return 'No unit of that service is free nearby. Try again shortly or pick another location.'
      case 'routing_unavailable':
        return 'Routing service is unavailable right now. Nothing was dispatched; please retry.'
      case 'invalid_incident':
        return err.message
      case 'unauthorized':
        return 'This action needs an API key. Set it with the key button in the header.'
      case 'rate_limited': {
        const s = err.details.retry_after_s
        return `Too many requests. Retry in ${typeof s === 'number' ? Math.ceil(s) : 'a few'} s.`
      }
      case 'network_error':
        return err.message
      case 'cannot_cancel_transport':
        return 'A patient is on board: a transport cannot be cancelled. The unit continues to the hospital.'
      case 'invalid_state':
        return 'This incident can no longer be cancelled (it has failed).'
      default:
        return err.message || `Request failed (${err.status})`
    }
  }
  return err instanceof Error ? err.message : 'Unexpected error'
}
