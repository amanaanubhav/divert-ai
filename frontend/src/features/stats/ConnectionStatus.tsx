import clsx from 'clsx'
import { useReady } from '../../api/queries'
import { formatClock } from '../../lib/format'
import { useLiveStore } from '../../store/live'

const LABEL = {
  idle: 'Idle',
  connecting: 'Connecting',
  open: 'Live',
  reconnecting: 'Reconnecting',
  closed: 'Offline',
  unknown_city: 'Unknown city',
} as const

/** Stream state, simulation clock (city local time) and worker health from `/readyz`. */
export function ConnectionStatus({ timeZone }: { timeZone: string }) {
  const conn = useLiveStore((s) => s.connection)
  const simTs = useLiveStore((s) => s.simTs)
  const ready = useReady()
  const worker = ready.data?.checks.worker
  const workerStatus = typeof worker === 'string' ? worker : worker?.status
  const degraded = ready.data?.degraded === true || ready.isError

  const tone =
    conn.status === 'open'
      ? 'bg-ok'
      : conn.status === 'connecting' || conn.status === 'reconnecting'
        ? 'bg-warn'
        : 'bg-bad'

  return (
    <div className="flex items-center gap-3 text-xs" aria-live="polite">
      <span className="inline-flex items-center gap-1.5 text-fg" title={`Stream: ${conn.status}`}>
        {/* A static status dot: colour + the text label next to it. */}
        <span aria-hidden="true" className={clsx('inline-block h-2 w-2 rounded-full', tone)} />
        {LABEL[conn.status]}
        {conn.status === 'reconnecting' && conn.retryInMs != null ? (
          <span className="text-muted">({Math.ceil(conn.retryInMs / 1000)} s)</span>
        ) : null}
      </span>
      <span
        className="hidden items-center gap-1 sm:inline-flex"
        title="Simulation clock (city local time)"
      >
        <span className="text-muted">Sim time</span>
        <span className="tabular-nums text-fg">{formatClock(simTs, timeZone)}</span>
      </span>
      {degraded ? (
        <span
          className="rounded border border-warn/40 bg-warn-subtle px-1.5 py-0.5 font-medium text-warn"
          title={
            ready.isError
              ? 'API not ready'
              : `Worker ${workerStatus ?? 'unknown'}: vehicles may pause; dispatches still queue`
          }
        >
          {ready.isError ? 'API not ready' : `Worker ${workerStatus ?? '?'}`}
        </span>
      ) : null}
    </div>
  )
}
