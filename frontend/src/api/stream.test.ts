import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { backoffDelay, parseMessage, ReconnectingStream, type ConnectionInfo } from './stream'
import type { StreamMessage } from './types'

class FakeSocket {
  static instances: FakeSocket[] = []
  readonly OPEN = 1
  readyState = 0
  sent: string[] = []
  closed: { code?: number } | null = null
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  constructor(readonly url: string) {
    FakeSocket.instances.push(this)
  }
  send(data: string) {
    this.sent.push(data)
  }
  close(code?: number) {
    this.closed = code === undefined ? {} : { code }
  }
  // test helpers
  open() {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }
  receive(data: unknown) {
    this.onmessage?.({
      data: typeof data === 'string' ? data : JSON.stringify(data),
    } as MessageEvent)
  }
  serverClose(code: number) {
    this.readyState = 3
    this.onclose?.({ code } as CloseEvent)
  }
  static last(): FakeSocket {
    const s = FakeSocket.instances[FakeSocket.instances.length - 1]
    if (!s) throw new Error('no socket')
    return s
  }
}

const SNAPSHOT = {
  type: 'snapshot',
  v: 1,
  city: 'delhi',
  tick: 3,
  sim_ts: null,
  vehicles: [],
  signals: [],
}

function makeStream(extra: Partial<ConstructorParameters<typeof ReconnectingStream>[0]> = {}) {
  const messages: StreamMessage[] = []
  const statuses: ConnectionInfo[] = []
  const stream = new ReconnectingStream({
    url: 'ws://test/api/v1/cities/delhi/stream',
    onMessage: (m) => messages.push(m),
    onStatus: (s) => statuses.push(s),
    initialDelayMs: 500,
    maxDelayMs: 4000,
    pingIntervalMs: 20_000,
    WebSocketImpl: FakeSocket as unknown as new (url: string) => WebSocket,
    random: () => 1, // deterministic: full delay
    ...extra,
  })
  return { stream, messages, statuses }
}

describe('parseMessage', () => {
  it('accepts known v1 messages and ignores the rest', () => {
    expect(parseMessage(JSON.stringify(SNAPSHOT))?.type).toBe('snapshot')
    expect(parseMessage(JSON.stringify({ type: 'something_new', v: 1 }))).toBeNull()
    expect(parseMessage(JSON.stringify({ type: 'tick', v: 2 }))).toBeNull() // future schema
    expect(parseMessage('not json')).toBeNull()
    expect(parseMessage(42)).toBeNull()
  })
})

describe('backoffDelay', () => {
  it('doubles up to the cap, with equal jitter', () => {
    expect(backoffDelay(0, 500, 4000, () => 1)).toBe(500)
    expect(backoffDelay(1, 500, 4000, () => 1)).toBe(1000)
    expect(backoffDelay(3, 500, 4000, () => 1)).toBe(4000)
    expect(backoffDelay(10, 500, 4000, () => 1)).toBe(4000)
    expect(backoffDelay(3, 500, 4000, () => 0)).toBe(2000) // never below half
  })
})

describe('ReconnectingStream', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeSocket.instances = []
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('connects, forwards messages and pings', () => {
    const { stream, messages, statuses } = makeStream()
    stream.start()
    expect(statuses.at(-1)?.status).toBe('connecting')
    const ws = FakeSocket.last()
    ws.open()
    expect(statuses.at(-1)?.status).toBe('open')
    ws.receive(SNAPSHOT)
    ws.receive({ type: 'unknown_future', v: 1 })
    expect(messages.map((m) => m.type)).toEqual(['snapshot'])
    vi.advanceTimersByTime(20_000)
    expect(ws.sent).toEqual([JSON.stringify({ type: 'ping' })])
    stream.stop()
  })

  it('reconnects with exponential backoff and resets after a snapshot', () => {
    const { stream, statuses } = makeStream()
    stream.start()
    FakeSocket.last().serverClose(1006)
    expect(statuses.at(-1)).toMatchObject({ status: 'reconnecting', attempt: 1, retryInMs: 500 })
    vi.advanceTimersByTime(499)
    expect(FakeSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeSocket.instances).toHaveLength(2)

    FakeSocket.last().serverClose(1006)
    expect(statuses.at(-1)?.retryInMs).toBe(1000)
    vi.advanceTimersByTime(1000)
    FakeSocket.last().serverClose(1013) // server asks for a resync
    expect(statuses.at(-1)?.retryInMs).toBe(2000)
    vi.advanceTimersByTime(2000)

    const ws = FakeSocket.last()
    ws.open()
    ws.receive(SNAPSHOT) // healthy session
    ws.serverClose(1006)
    expect(statuses.at(-1)?.retryInMs).toBe(500)
    stream.stop()
  })

  it('never reconnects to an unknown city (4404)', () => {
    const { stream, statuses } = makeStream()
    stream.start()
    FakeSocket.last().serverClose(4404)
    expect(statuses.at(-1)?.status).toBe('unknown_city')
    vi.advanceTimersByTime(60_000)
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it('stop() closes the socket and cancels a pending retry', () => {
    const { stream } = makeStream()
    stream.start()
    FakeSocket.last().serverClose(1006)
    stream.stop()
    vi.advanceTimersByTime(60_000)
    expect(FakeSocket.instances).toHaveLength(1)

    const s2 = makeStream()
    s2.stream.start()
    const ws = FakeSocket.last()
    ws.open()
    s2.stream.stop()
    expect(ws.closed).toEqual({ code: 1000 })
  })
})
