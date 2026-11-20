import { STREAM_SCHEMA_VERSION, WS_CLOSE_UNKNOWN_CITY, type StreamMessage } from './types'

export type ConnectionStatus =
  'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed' | 'unknown_city'

export interface ConnectionInfo {
  status: ConnectionStatus
  /** Consecutive failed attempts since the last good session. */
  attempt: number
  /** Delay before the next attempt (only while `reconnecting`). */
  retryInMs?: number
}

type SocketCtor = new (url: string) => WebSocket

export interface StreamOptions {
  url: string
  onMessage: (msg: StreamMessage) => void
  onStatus?: (info: ConnectionInfo) => void
  initialDelayMs?: number
  maxDelayMs?: number
  pingIntervalMs?: number
  /** Injection points for tests. */
  WebSocketImpl?: SocketCtor
  random?: () => number
}

const KNOWN_TYPES = new Set([
  'snapshot',
  'tick',
  'vehicle_added',
  'leg_changed',
  'route_replaced',
  'vehicle_removed',
  'incident_updated',
  'pong',
])

/** Parse one frame; `null` for anything a v1 client must ignore. */
export function parseMessage(raw: unknown): StreamMessage | null {
  if (typeof raw !== 'string') return null
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch {
    return null
  }
  if (typeof data !== 'object' || data === null) return null
  const { type, v } = data as { type?: unknown; v?: unknown }
  if (typeof type !== 'string' || !KNOWN_TYPES.has(type)) return null
  if (typeof v === 'number' && v > STREAM_SCHEMA_VERSION) return null
  return data as StreamMessage
}

/** Exponential backoff with "equal jitter": half fixed, half random. */
export function backoffDelay(
  attempt: number,
  initialMs: number,
  maxMs: number,
  random: () => number = Math.random,
): number {
  const exp = Math.min(maxMs, initialMs * 2 ** Math.max(0, attempt))
  return Math.round(exp / 2 + (random() * exp) / 2)
}

/**
 * A WebSocket that reconnects with exponential backoff, pings to keep proxies from idling
 * the connection out, and never reconnects to an unknown city (close code 4404). Every
 * successful (re)connect starts with a fresh `snapshot`, so consumers simply replace state.
 */
export class ReconnectingStream {
  private ws: WebSocket | null = null
  private attempt = 0
  private stopped = true
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private pingTimer: ReturnType<typeof setInterval> | null = null
  private readonly opts: Required<Omit<StreamOptions, 'onStatus'>> & Pick<StreamOptions, 'onStatus'>

  constructor(opts: StreamOptions) {
    this.opts = {
      initialDelayMs: 500,
      maxDelayMs: 15_000,
      pingIntervalMs: 20_000,
      WebSocketImpl: WebSocket,
      random: Math.random,
      ...opts,
    }
  }

  start(): void {
    if (!this.stopped) return
    this.stopped = false
    this.attempt = 0
    if (typeof window !== 'undefined') window.addEventListener('online', this.onOnline)
    this.connect()
  }

  stop(): void {
    this.stopped = true
    if (typeof window !== 'undefined') window.removeEventListener('online', this.onOnline)
    this.clearTimers()
    const ws = this.ws
    this.ws = null
    if (ws) {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null
      try {
        ws.close(1000, 'client closed')
      } catch {
        // already closed
      }
    }
    this.emit({ status: 'closed', attempt: this.attempt })
  }

  private readonly onOnline = (): void => {
    if (!this.stopped && this.retryTimer !== null) {
      clearTimeout(this.retryTimer)
      this.retryTimer = null
      this.connect()
    }
  }

  private emit(info: ConnectionInfo): void {
    this.opts.onStatus?.(info)
  }

  private clearTimers(): void {
    if (this.retryTimer !== null) clearTimeout(this.retryTimer)
    if (this.pingTimer !== null) clearInterval(this.pingTimer)
    this.retryTimer = null
    this.pingTimer = null
  }

  private connect(): void {
    if (this.stopped) return
    this.emit({ status: this.attempt === 0 ? 'connecting' : 'reconnecting', attempt: this.attempt })
    let ws: WebSocket
    try {
      ws = new this.opts.WebSocketImpl(this.opts.url)
    } catch {
      this.scheduleReconnect()
      return
    }
    this.ws = ws
    ws.onopen = () => {
      this.emit({ status: 'open', attempt: this.attempt })
      this.pingTimer = setInterval(() => {
        if (ws.readyState === ws.OPEN) ws.send(JSON.stringify({ type: 'ping' }))
      }, this.opts.pingIntervalMs)
    }
    ws.onmessage = (ev: MessageEvent) => {
      const msg = parseMessage(ev.data)
      if (!msg) return
      if (msg.type === 'snapshot') this.attempt = 0 // a healthy session resets the backoff
      this.opts.onMessage(msg)
    }
    ws.onerror = () => {
      // onclose follows and decides what to do
    }
    ws.onclose = (ev: CloseEvent) => {
      if (this.ws !== ws) return
      this.ws = null
      if (this.pingTimer !== null) clearInterval(this.pingTimer)
      this.pingTimer = null
      if (this.stopped) return
      if (ev.code === WS_CLOSE_UNKNOWN_CITY) {
        this.stopped = true
        this.emit({ status: 'unknown_city', attempt: this.attempt })
        return
      }
      this.scheduleReconnect()
    }
  }

  private scheduleReconnect(): void {
    const delay = backoffDelay(
      this.attempt,
      this.opts.initialDelayMs,
      this.opts.maxDelayMs,
      this.opts.random,
    )
    this.attempt += 1
    this.emit({ status: 'reconnecting', attempt: this.attempt, retryInMs: delay })
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null
      this.connect()
    }, delay)
  }
}
