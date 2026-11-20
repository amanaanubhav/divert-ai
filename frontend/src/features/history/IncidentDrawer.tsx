import { useEffect, useRef } from 'react'
import { useIncidentDetail, useTrack } from '../../api/queries'
import type { DispatchRecord, IncidentDetail, TimelineEvent } from '../../api/types'
import { ServiceDot, StatusBadge, TypeBadge } from '../../components/ui/Badges'
import { Spinner } from '../../components/ui/Panel'
import {
  formatClock,
  formatDateTime,
  formatDistance,
  formatDuration,
  formatPercent,
  formatSeconds,
  secondsBetween,
} from '../../lib/format'
import { TrackReplay } from './TrackReplay'

const TIMELINE_LABEL: Record<string, string> = {
  reported: 'Reported',
  dispatched: 'Unit dispatched',
  cancel_requested: 'Cancel requested',
  en_route: 'En route',
  preemption_claimed: 'Signal pre-empted',
  preemption_conflict: 'Signal conflict',
  preemption_released: 'Signal released',
  arrived: 'Arrived on scene',
  cleared: 'Scene cleared',
  cancelled: 'Cancelled',
  failed: 'Failed',
}

const TIMELINE_DOT: Record<string, string> = {
  preemption_claimed: 'bg-emerald-400',
  preemption_released: 'bg-slate-500',
  preemption_conflict: 'bg-amber-400',
  arrived: 'bg-amber-300',
  cleared: 'bg-emerald-300',
  failed: 'bg-red-500',
  cancelled: 'bg-slate-400',
}

/** Detail of one incident: dispatch facts, predicted vs actual, pre-emptions, timeline, replay. */
export function IncidentDrawer({
  incidentId,
  timeZone,
  onClose,
}: {
  incidentId: string
  timeZone: string
  onClose: () => void
}) {
  const detail = useIncidentDetail(incidentId)
  const track = useTrack(incidentId)
  const closeRef = useRef<HTMLButtonElement>(null)
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null
    closeRef.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      previouslyFocused?.focus()
    }
  }, [])

  const d = detail.data
  return (
    <div className="fixed inset-0 z-[1500] flex justify-end">
      <button
        type="button"
        aria-label="Close incident detail"
        tabIndex={-1}
        className="absolute inset-0 cursor-default bg-black/50"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="incident-drawer-title"
        className="relative flex h-full w-full max-w-2xl animate-slide-in flex-col bg-ink-900 shadow-2xl ring-1 ring-white/10"
      >
        <header className="flex items-start justify-between gap-3 border-b border-white/[0.06] px-4 py-3">
          <div className="min-w-0">
            <h2
              id="incident-drawer-title"
              className="flex flex-wrap items-center gap-2 text-base font-semibold text-slate-100"
            >
              {d ? <TypeBadge type={d.type} /> : 'Incident'}
              {d ? <StatusBadge status={d.status} /> : null}
            </h2>
            <p className="mt-0.5 truncate font-mono text-[11px] text-slate-500">{incidentId}</p>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className="rounded-md px-2 py-1 text-slate-400 hover:bg-white/10 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
            aria-label="Close"
          >
            ✕
          </button>
        </header>
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-4">
          {detail.isLoading ? <Spinner label="Loading incident" /> : null}
          {detail.isError ? (
            <p className="text-sm text-red-300">Could not load this incident.</p>
          ) : null}
          {d ? (
            <>
              <Summary d={d} timeZone={timeZone} />
              {d.dispatches.map((dp) => (
                <DispatchCard key={dp.id} dp={dp} />
              ))}
              <section aria-labelledby="replay-h">
                <h3
                  id="replay-h"
                  className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400"
                >
                  Route & replay
                </h3>
                {track.isLoading ? (
                  <Spinner label="Loading track" />
                ) : (
                  <TrackReplay detail={d} points={track.data?.points ?? []} />
                )}
              </section>
              <Preemptions d={d} timeZone={timeZone} />
              <Timeline events={d.timeline} timeZone={timeZone} />
            </>
          ) : null}
        </div>
      </div>
    </div>
  )
}

function Summary({ d, timeZone }: { d: IncidentDetail; timeZone: string }) {
  const rows: [string, string | null][] = [
    ['Reported', d.reported_at],
    ['Dispatched', d.dispatched_at],
    ['En route', d.en_route_at],
    ['On scene', d.on_scene_at],
    ['Resolved', d.resolved_at],
    ['Cancelled', d.cancelled_at],
    ['Failed', d.failed_at],
  ]
  const response = secondsBetween(d.reported_at, d.on_scene_at)
  return (
    <section aria-label="Summary" className="space-y-2">
      {d.description ? (
        <p className="rounded-md bg-ink-800 px-3 py-2 text-sm text-slate-300">{d.description}</p>
      ) : null}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-3">
        {rows
          .filter(([, v]) => v)
          .map(([k, v]) => (
            <div key={k}>
              <dt className="text-[11px] text-slate-500">{k}</dt>
              <dd className="font-mono text-xs text-slate-200">{formatClock(v, timeZone)}</dd>
            </div>
          ))}
        <div>
          <dt className="text-[11px] text-slate-500">Response time</dt>
          <dd className="font-mono text-xs text-rose-200">{formatDuration(response)}</dd>
        </div>
      </dl>
      <p className="text-[11px] text-slate-500">
        Reported {formatDateTime(d.reported_at, timeZone)} ({timeZone}) as “{d.reported_type}”
        {d.failure_reason ? <span className="text-red-300"> · {d.failure_reason}</span> : null}
      </p>
    </section>
  )
}

function DispatchCard({ dp }: { dp: DispatchRecord }) {
  const predicted = dp.predicted_eta_s
  const actual = dp.actual_travel_s
  const max = Math.max(predicted, actual ?? 0, 1)
  const err = actual != null && predicted > 0 ? (actual - predicted) / predicted : null
  return (
    <section
      aria-label={`Dispatch ${dp.vehicle_id}`}
      className="rounded-lg bg-ink-800/60 p-3 ring-1 ring-inset ring-white/[0.05]"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-2">
          <ServiceDot service={dp.service} />
          <span className="font-mono text-sm font-semibold text-slate-100">{dp.vehicle_id}</span>
        </span>
        <span className="text-xs text-slate-400">
          {dp.end_reason ? dp.end_reason.toLowerCase() : 'active'}
        </span>
      </div>
      <p className="mt-0.5 text-xs text-slate-500">
        from {dp.station.name ?? dp.station.id} · {formatDistance(dp.distance_m)} ·{' '}
        {dp.corridor?.length ?? 0} corridor junctions · model {dp.eta_model ?? '–'}
      </p>
      <div
        className="mt-3 space-y-1.5"
        role="group"
        aria-label="Predicted versus actual travel time"
      >
        <Bar label="Predicted ETA" value={predicted} max={max} className="bg-sky-400" />
        <Bar label="Actual travel" value={actual} max={max} className="bg-rose-400" />
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 text-xs text-slate-400">
        <span>
          error{' '}
          <span
            className={
              err == null ? '' : Math.abs(err) <= 0.1 ? 'text-emerald-300' : 'text-amber-300'
            }
          >
            {err == null ? '–' : `${err > 0 ? '+' : ''}${formatPercent(err)}`}
          </span>
        </span>
        <span title="Estimate: pre-empted junctions passed × city average junction delay">
          est. time saved{' '}
          <span className="text-emerald-300">{formatSeconds(dp.est_time_saved_s)}</span>{' '}
          <span className="text-slate-600">(estimate)</span>
        </span>
      </div>
    </section>
  )
}

function Bar({
  label,
  value,
  max,
  className,
}: {
  label: string
  value: number | null
  max: number
  className: string
}) {
  return (
    <div className="grid grid-cols-[6.5rem,1fr,3.5rem] items-center gap-2 text-xs">
      <span className="text-slate-400">{label}</span>
      <span className="h-2 overflow-hidden rounded-full bg-ink-700">
        <span
          className={`block h-full rounded-full ${className}`}
          style={{ width: `${value == null ? 0 : (value / max) * 100}%` }}
        />
      </span>
      <span className="text-right font-mono tabular-nums text-slate-200">
        {formatDuration(value)}
      </span>
    </div>
  )
}

function Preemptions({ d, timeZone }: { d: IncidentDetail; timeZone: string }) {
  return (
    <section aria-labelledby="pre-h">
      <h3 id="pre-h" className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
        Signal pre-emptions · {d.preemptions.length}
      </h3>
      {d.preemptions.length === 0 ? (
        <p className="text-xs text-slate-500">No junction was pre-empted for this incident.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-[10px] uppercase tracking-wider text-slate-500">
              <tr>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Signal
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  State
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Lane
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  ETA at claim
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Claimed
                </th>
                <th scope="col" className="py-1 font-medium">
                  Held
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/[0.05]">
              {d.preemptions.map((p) => (
                <tr key={p.event_id}>
                  <td className="py-1 pr-3 font-mono text-slate-200">{p.signal_id}</td>
                  <td
                    className={`py-1 pr-3 ${p.state === 'CONFLICT' ? 'text-amber-300' : 'text-emerald-300'}`}
                  >
                    {p.state.toLowerCase()}
                  </td>
                  <td className="py-1 pr-3 text-slate-300">{p.lane ?? '–'}</td>
                  <td className="py-1 pr-3 font-mono text-slate-300">
                    {formatSeconds(p.eta_at_claim_s)}
                  </td>
                  <td className="py-1 pr-3 font-mono text-slate-400">
                    {formatClock(p.claimed_at, timeZone)}
                  </td>
                  <td className="py-1 font-mono text-slate-400">
                    {formatSeconds(secondsBetween(p.claimed_at, p.released_at))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function Timeline({ events, timeZone }: { events: readonly TimelineEvent[]; timeZone: string }) {
  return (
    <section aria-labelledby="tl-h">
      <h3 id="tl-h" className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
        Timeline
      </h3>
      <ol className="relative ml-1 space-y-2 border-l border-white/10 pl-4">
        {events.map((e) => (
          <li key={e.event_id} className="relative">
            <span
              aria-hidden="true"
              className={`absolute -left-[21px] top-1 h-2.5 w-2.5 rounded-full ring-2 ring-ink-900 ${TIMELINE_DOT[e.type] ?? 'bg-rose-400'}`}
            />
            <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
              <span className="font-mono text-[11px] text-slate-500">
                {formatClock(e.ts, timeZone)}
              </span>
              <span className="text-slate-200">{TIMELINE_LABEL[e.type] ?? e.type}</span>
              <span className="text-xs text-slate-500">{payloadSummary(e)}</span>
            </div>
          </li>
        ))}
      </ol>
    </section>
  )
}

function payloadSummary(e: TimelineEvent): string {
  const p = e.payload ?? {}
  const parts: string[] = []
  const str = (k: string) =>
    typeof p[k] === 'string' || typeof p[k] === 'number' ? String(p[k]) : null
  const sig = str('signal_id')
  if (sig) parts.push(sig)
  const vid = str('vehicle_id')
  if (vid && !sig) parts.push(vid)
  const lane = str('lane')
  if (lane) parts.push(`lane ${lane}`)
  const reason = str('reason')
  if (reason) parts.push(reason)
  const travel = p.travel_s
  if (typeof travel === 'number') parts.push(`travel ${formatDuration(travel)}`)
  return parts.join(' · ')
}
