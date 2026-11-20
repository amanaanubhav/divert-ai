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
      ? 'bg-emerald-400'
      : conn.status === 'connecting' || conn.status === 'reconnecting'
        ? 'bg-amber-400'
        : 'bg-red-500'

  return (
    <div className="flex items-center gap-3 text-xs" aria-live="polite">
      <span
        className="inline-flex items-center gap-1.5 text-slate-300"
        title={`Stream: ${conn.status}`}
      >
        <span className="relative flex h-2 w-2">
          {conn.status === 'open' ? (
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
          ) : null}
          <span className={clsx('relative inline-flex h-2 w-2 rounded-full', tone)} />
        </span>
        {LABEL[conn.status]}
        {conn.status === 'reconnecting' && conn.retryInMs != null ? (
          <span className="text-slate-500">({Math.ceil(conn.retryInMs / 1000)} s)</span>
        ) : null}
      </span>
      <span
        className="hidden items-center gap-1 sm:inline-flex"
        title="Simulation clock (city local time)"
      >
        <span className="text-slate-500">sim</span>
        <span className="font-mono tabular-nums text-slate-200">
          {formatClock(simTs, timeZone)}
        </span>
      </span>
      {degraded ? (
        <span
          className="rounded bg-amber-500/15 px-1.5 py-0.5 font-medium text-amber-300 ring-1 ring-inset ring-amber-400/30"
          title={
            ready.isError
              ? 'API not ready'
              : `Worker ${workerStatus ?? 'unknown'}: vehicles may pause; dispatches still queue`
          }
        >
          {ready.isError ? 'API not ready' : `worker ${workerStatus ?? '?'}`}
        </span>
      ) : null}
    </div>
  )
}
