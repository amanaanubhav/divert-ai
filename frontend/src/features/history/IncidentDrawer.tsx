import { useEffect, useMemo, useRef, useState } from 'react'
import { useIncidentDetail, useTrack } from '../../api/queries'
import type {
  DispatchRecord,
  IncidentDetail,
  LegRecord,
  ServiceType,
  TimelineEvent,
} from '../../api/types'
import {
  DispatchStatusBadge,
  LegTag,
  ServiceDot,
  StatusBadge,
  TypeBadge,
} from '../../components/ui/Badges'
import { Button } from '../../components/ui/Button'
import { Spinner } from '../../components/ui/Panel'
import {
  DEST_KIND_LABEL,
  edLabel,
  formatClock,
  formatDateTime,
  formatDistance,
  formatDuration,
  formatSeconds,
  formatSignedPct,
  hospitalLabel,
  LEG_END_LABEL,
  LEG_LABEL,
  MOVEMENT_LABEL,
  OUTCOME_LABEL,
  secondsBetween,
} from '../../lib/format'
import type { RoutePreview } from '../../store/ui'
import { RouteChoiceExplainer } from '../route-choice/RouteChoiceExplainer'
import { legErrorPct, legOf, payloadSummary, timelineLegs, tripTimes } from './drawerText'
import { drawableLegs } from './replay'
import { TrackReplay } from './TrackReplay'

/** The ETA guarantee of the design (predicted within +-10 % of actual, every leg kind). */
const ETA_TOLERANCE_PCT = 10

const TIMELINE_LABEL: Record<string, string> = {
  reported: 'Reported',
  dispatched: 'Unit dispatched',
  cancel_requested: 'Cancel requested',
  en_route: 'En route',
  preemption_claimed: 'Signal pre-empted',
  preemption_conflict: 'Signal conflict (queued)',
  preempt_transition: 'Signal transitioning to green',
  preempt_green: 'Signal green for the vehicle',
  preempt_timeout: 'Signal hold timed out',
  preemption_released: 'Signal released',
  arrived: 'Arrived on scene',
  cleared: 'Scene cleared',
  leg_planned: 'Leg planned',
  leg_plan_failed: 'Leg planning failed (retrying)',
  leg_started: 'Leg started',
  leg_ended: 'Leg ended',
  rerouted: 'Re-routed',
  transport_started: 'Transport started (patient on board)',
  arrived_hospital: 'Arrived at hospital',
  handover_complete: 'Patient handed over',
  returning: 'Released, returning',
  back_in_service: 'Back in service',
  cancelled: 'Cancelled',
  cancel_rejected: 'Cancel refused',
  failed: 'Failed',
}

const TIMELINE_DOT: Record<string, string> = {
  preemption_claimed: 'bg-ok',
  preempt_green: 'bg-ok',
  preemption_released: 'bg-muted',
  preemption_conflict: 'bg-warn',
  preempt_timeout: 'bg-warn',
  leg_plan_failed: 'bg-warn',
  arrived: 'bg-warn',
  cleared: 'bg-ok',
  transport_started: 'bg-transport',
  arrived_hospital: 'bg-transport',
  handover_complete: 'bg-transport',
  back_in_service: 'bg-ok',
  cancel_rejected: 'bg-warn',
  failed: 'bg-bad',
  cancelled: 'bg-muted',
}

/** Detail of one incident: legs, predicted vs actual, hospital, pre-emptions, timeline, replay. */
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
  const [preview, setPreview] = useState<RoutePreview[] | null>(null)
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
  const legs = useMemo(() => (d ? drawableLegs(d) : []), [d])
  return (
    <div className="fixed inset-0 z-drawer flex justify-end">
      <button
        type="button"
        aria-label="Close incident detail"
        tabIndex={-1}
        className="absolute inset-0 cursor-default bg-scrim/40"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="incident-drawer-title"
        className="relative flex h-full w-full max-w-2xl flex-col border-l border-line bg-surface shadow"
      >
        <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
          <div className="min-w-0">
            <h2
              id="incident-drawer-title"
              className="flex flex-wrap items-center gap-2 text-base font-semibold text-fg"
            >
              {d ? <TypeBadge type={d.type} /> : 'Incident'}
              {d ? <StatusBadge status={d.status} /> : null}
            </h2>
            <p className="mt-0.5 truncate font-mono text-xs text-muted">{incidentId}</p>
          </div>
          <Button ref={closeRef} size="sm" onClick={onClose} aria-label="Close">
            Close
          </Button>
        </header>
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-4">
          {detail.isLoading ? <Spinner label="Loading incident" /> : null}
          {detail.isError ? (
            <p className="text-sm text-bad">Could not load this incident.</p>
          ) : null}
          {d ? (
            <>
              <Summary d={d} legs={legs} timeZone={timeZone} />
              {d.dispatches.map((dp) => (
                <DispatchCard key={dp.id} dp={dp} />
              ))}
              <section aria-labelledby="replay-h">
                <h3 id="replay-h" className="mb-2 text-sm font-semibold text-fg">
                  Route and replay
                </h3>
                {track.isLoading ? (
                  <Spinner label="Loading track" />
                ) : (
                  <TrackReplay detail={d} points={track.data?.points ?? []} preview={preview} />
                )}
              </section>
              <Legs d={d} legs={legs} timeZone={timeZone} onPreview={setPreview} />
              <Hospital d={d} legs={legs} />
              <Preemptions d={d} legs={legs} timeZone={timeZone} />
              <Timeline events={d.timeline} legs={legs} timeZone={timeZone} />
            </>
          ) : null}
        </div>
      </div>
    </div>
  )
}

function Summary({
  d,
  legs,
  timeZone,
}: {
  d: IncidentDetail
  legs: readonly LegRecord[]
  timeZone: string
}) {
  const rows: [string, string | null | undefined][] = [
    ['Reported', d.reported_at],
    ['Dispatched', d.dispatched_at],
    ['En route', d.en_route_at],
    ['On scene', d.on_scene_at],
    ['Transport started', d.transport_started_at],
    ['At hospital', d.at_hospital_at],
    ['Resolved', d.resolved_at],
    ['Cancelled', d.cancelled_at],
    ['Failed', d.failed_at],
  ]
  const { responseS, callToHospitalS } = tripTimes(d, legs)
  return (
    <section aria-label="Summary" className="space-y-2">
      {d.description ? (
        <p className="rounded border border-line bg-subtle px-3 py-2 text-sm text-fg">
          {d.description}
        </p>
      ) : null}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-3">
        {rows
          .filter(([, v]) => v)
          .map(([k, v]) => (
            <div key={k}>
              <dt className="text-xs text-muted">{k}</dt>
              <dd className="tabular-nums text-fg">{formatClock(v, timeZone)}</dd>
            </div>
          ))}
        <div>
          <dt className="text-xs text-muted">Response time</dt>
          <dd className="font-semibold tabular-nums text-fg">{formatDuration(responseS)}</dd>
        </div>
        {callToHospitalS != null ? (
          <div>
            <dt className="text-xs text-muted">Call to hospital</dt>
            <dd className="font-semibold tabular-nums text-fg">
              {formatDuration(callToHospitalS)}
            </dd>
          </div>
        ) : null}
      </dl>
      <p className="text-xs text-muted">
        Reported {formatDateTime(d.reported_at, timeZone)} ({timeZone}) as “{d.reported_type}”.
        Travel times are simulated seconds.
        {d.failure_reason ? <span className="text-bad"> · {d.failure_reason}</span> : null}
      </p>
    </section>
  )
}

function DispatchCard({ dp }: { dp: DispatchRecord }) {
  return (
    <section aria-label={`Dispatch ${dp.vehicle_id}`} className="rounded border border-line p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-2">
          <ServiceDot service={dp.service} />
          <span className="font-mono text-sm font-semibold text-fg">{dp.vehicle_id}</span>
        </span>
        {dp.status ? (
          <DispatchStatusBadge status={dp.status} />
        ) : (
          <span className="text-xs text-muted">
            {dp.end_reason ? dp.end_reason.toLowerCase() : 'active'}
          </span>
        )}
      </div>
      <p className="mt-0.5 text-xs text-muted">
        Unit of {dp.station.name ?? dp.station.id} · model {dp.eta_model ?? '–'}
        {dp.end_reason ? ` · ended: ${dp.end_reason.toLowerCase()}` : ''}
      </p>
      <p
        className="mt-1 text-xs text-muted"
        title="Pre-empted junctions passed × city average junction delay"
      >
        est. time saved <span className="text-ok">{formatSeconds(dp.est_time_saved_s)}</span>{' '}
        (estimate)
      </p>
    </section>
  )
}

function Legs({
  d,
  legs,
  timeZone,
  onPreview,
}: {
  d: IncidentDetail
  legs: readonly LegRecord[]
  timeZone: string
  onPreview: (p: RoutePreview[] | null) => void
}) {
  const service = (id: string): ServiceType =>
    d.dispatches.find((x) => x.id === id)?.service ?? 'AMBULANCE'
  return (
    <section aria-labelledby="legs-h">
      <h3 id="legs-h" className="mb-2 text-sm font-semibold text-fg">
        Trip legs · {legs.length}
      </h3>
      {legs.length === 0 ? (
        <p className="text-sm text-muted">No legs recorded.</p>
      ) : (
        <ol className="space-y-3">
          {legs.map((l) => (
            <li key={`${l.dispatch_id}:${l.seq}`}>
              <LegCard
                l={l}
                service={service(l.dispatch_id)}
                preemptions={
                  d.preemptions.filter(
                    (p) => p.dispatch_id === l.dispatch_id && (p.leg_seq ?? 0) === l.seq,
                  ).length
                }
                timeZone={timeZone}
                onPreview={onPreview}
              />
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function LegCard({
  l,
  service,
  preemptions,
  timeZone,
  onPreview,
}: {
  l: LegRecord
  service: ServiceType
  preemptions: number
  timeZone: string
  onPreview: (p: RoutePreview[] | null) => void
}) {
  const predicted = l.predicted_eta_s
  const actual = l.actual_travel_s
  const max = Math.max(predicted, actual ?? 0, 1)
  const err = legErrorPct(l)
  const dest = l.destination
  const freeLefts = (l.corridor ?? []).filter((c) => c.free_left).length
  return (
    <article
      aria-label={`${LEG_LABEL[l.kind]} leg`}
      className="space-y-2 rounded border border-line p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-2">
          <LegTag kind={l.kind} service={service} />
          <span className="text-xs text-muted">
            to{' '}
            {dest.kind === 'SCENE'
              ? 'the scene'
              : `${dest.name ?? dest.station_id ?? '–'} (${DEST_KIND_LABEL[dest.kind]})`}
          </span>
        </span>
        <span className="text-xs text-muted">
          {l.end_reason ? LEG_END_LABEL[l.end_reason] : l.started_at ? 'being driven' : 'planned'}
          {l.started_at ? ` · ${formatClock(l.started_at, timeZone)}` : ''}
        </span>
      </div>
      <p className="text-xs text-muted">
        {formatDistance(l.distance_m)} ·{' '}
        {l.emergency ? 'emergency (pre-empts signals)' : 'obeys signals'} ·{' '}
        {(l.corridor ?? []).length} corridor junctions
        {freeLefts > 0 ? ` (${freeLefts} free left)` : ''} · {preemptions} pre-empted
        {l.reroutes > 0 ? ` · re-routed ${l.reroutes}×` : ''} · model {l.eta_model ?? '–'}
      </p>
      {l.end_reason === 'SUPERSEDED' ? null : (
        <div
          className="space-y-1.5"
          role="group"
          aria-label={`${LEG_LABEL[l.kind]} leg: predicted versus actual travel time`}
        >
          <Bar label="Predicted ETA" value={predicted} max={max} className="bg-field" />
          <Bar label="Actual travel" value={actual} max={max} className="bg-accent" />
          <p className="text-xs text-muted">
            error{' '}
            <span
              className={
                err == null ? '' : Math.abs(err) <= ETA_TOLERANCE_PCT ? 'text-ok' : 'text-warn'
              }
            >
              {formatSignedPct(err)}
            </span>
            {err == null
              ? actual == null
                ? ' (not arrived yet)'
                : ''
              : Math.abs(err) <= ETA_TOLERANCE_PCT
                ? ' (within ±10 %)'
                : ' (outside ±10 %)'}
            {l.reroutes > 0 ? ' · re-routed legs are left out of the ETA accuracy metric' : ''}
          </p>
        </div>
      )}
      {l.route_choice ? (
        <RouteChoiceExplainer choice={l.route_choice} onPreview={onPreview} />
      ) : null}
    </article>
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
      <span className="text-muted">{label}</span>
      <span className="h-2 overflow-hidden rounded-sm bg-subtle">
        <span
          className={`block h-full ${className}`}
          style={{ width: `${value == null ? 0 : (value / max) * 100}%` }}
        />
      </span>
      <span className="text-right tabular-nums text-fg">{formatDuration(value)}</span>
    </div>
  )
}

function Hospital({ d, legs }: { d: IncidentDetail; legs: readonly LegRecord[] }) {
  const choice = legOf(legs, 'TRANSPORT')?.hospital_choice ?? null
  if (!d.hospital && !choice) return null
  const sorted = choice
    ? [...choice.candidates].sort((a, b) => (a.cost_s ?? Infinity) - (b.cost_s ?? Infinity))
    : []
  return (
    <section aria-labelledby="hosp-h" className="space-y-2">
      <h3 id="hosp-h" className="text-sm font-semibold text-fg">
        Hospital
      </h3>
      {d.hospital ? (
        <p className="text-sm text-fg">
          <span className="font-medium">{hospitalLabel(d.hospital)}</span>{' '}
          <span className="text-muted">· {edLabel(d.hospital.emergency)}</span>
        </p>
      ) : null}
      {choice ? (
        <>
          <p className="text-xs text-fg">{choice.reason}</p>
          <details className="rounded border border-line">
            <summary className="cursor-pointer select-none px-2 py-1 text-xs font-medium text-accent">
              Hospitals considered · {choice.candidates.length}
            </summary>
            <div className="overflow-x-auto border-t border-line px-2 py-1">
              <table className="w-full text-left text-xs">
                <thead className="text-muted">
                  <tr>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      Hospital
                    </th>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      Emergency
                    </th>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      ETA
                    </th>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      Cost
                    </th>
                    <th scope="col" className="py-1 font-medium">
                      Distance
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {sorted.map((c) => {
                    const chosen = c.station_id === choice.chosen
                    return (
                      <tr key={c.station_id} className={chosen ? 'font-medium' : ''}>
                        <td className="py-1 pr-3 text-fg">
                          {hospitalLabel(c)}
                          {chosen ? <span className="text-ok"> · chosen</span> : null}
                          {c.error ? <span className="text-warn"> · {c.error}</span> : null}
                        </td>
                        <td className="py-1 pr-3 text-muted">{c.emergency ?? 'not tagged'}</td>
                        <td className="py-1 pr-3 tabular-nums text-fg">
                          {formatDuration(c.eta_s)}
                        </td>
                        <td className="py-1 pr-3 tabular-nums text-fg">
                          {formatDuration(c.cost_s)}
                        </td>
                        <td className="py-1 tabular-nums text-fg">
                          {formatDistance(c.distance_m)}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              <p className="py-1 text-xs text-muted">
                Choice uses the OSM emergency tag only: no bed or capacity data
                {choice.ed_tolerance_s != null
                  ? `; a confirmed emergency department wins within ${formatDuration(choice.ed_tolerance_s)}.`
                  : '.'}
              </p>
            </div>
          </details>
        </>
      ) : null}
    </section>
  )
}

function Preemptions({
  d,
  legs,
  timeZone,
}: {
  d: IncidentDetail
  legs: readonly LegRecord[]
  timeZone: string
}) {
  const kindOf = (dispatchId: string, seq: number | null | undefined) =>
    legs.find((l) => l.dispatch_id === dispatchId && l.seq === (seq ?? 0))?.kind
  const green = d.preemptions.filter((p) => p.phase_state_at_claim === 'green_extend').length
  const red = d.preemptions.filter((p) => p.phase_state_at_claim === 'red_truncate').length
  return (
    <section aria-labelledby="pre-h">
      <h3 id="pre-h" className="mb-2 text-sm font-semibold text-fg">
        Signal pre-emptions · {d.preemptions.length}
      </h3>
      {d.preemptions.length === 0 ? (
        <p className="text-sm text-muted">No junction was pre-empted for this incident.</p>
      ) : (
        <>
          {green + red > 0 ? (
            <p className="mb-1 text-xs text-muted">
              {green} green extended · {red} red truncated
            </p>
          ) : null}
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs text-muted">
                <tr>
                  {['Signal', 'Leg', 'Movement', 'Lane', 'State', 'Outcome', 'Claimed', 'Held'].map(
                    (h) => (
                      <th key={h} scope="col" className="py-1 pr-3 font-medium last:pr-0">
                        {h}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {d.preemptions.map((p) => {
                  const kind = kindOf(p.dispatch_id, p.leg_seq)
                  return (
                    <tr key={p.event_id}>
                      <td className="py-1 pr-3 font-mono text-xs text-fg">{p.signal_id}</td>
                      <td className="py-1 pr-3 text-xs text-fg">{kind ? LEG_LABEL[kind] : '–'}</td>
                      <td className="py-1 pr-3 text-xs text-fg">
                        {p.movement ? MOVEMENT_LABEL[p.movement] : '–'}
                      </td>
                      <td className="py-1 pr-3 text-xs text-fg">
                        {p.lane_index != null && p.lanes_total != null
                          ? `${p.lane_index} of ${p.lanes_total}`
                          : (p.lane ?? '–')}
                      </td>
                      <td
                        className={`py-1 pr-3 text-xs ${p.state === 'CONFLICT' ? 'text-warn' : 'text-ok'}`}
                      >
                        {p.state === 'CONFLICT' ? 'queued' : 'pre-empted'}
                        {p.phase_state_at_claim
                          ? ` · ${p.phase_state_at_claim === 'green_extend' ? 'green ext.' : 'red trunc.'}`
                          : ''}
                      </td>
                      <td className="py-1 pr-3 text-xs text-fg">
                        {p.outcome ? OUTCOME_LABEL[p.outcome] : '–'}
                        {p.waited_s ? (
                          <span className="text-muted"> · waited {formatSeconds(p.waited_s)}</span>
                        ) : null}
                      </td>
                      <td className="py-1 pr-3 tabular-nums text-xs text-muted">
                        {formatClock(p.claimed_at, timeZone)}
                      </td>
                      <td className="py-1 tabular-nums text-xs text-muted">
                        {formatSeconds(secondsBetween(p.claimed_at, p.released_at))}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}

function Timeline({
  events,
  legs,
  timeZone,
}: {
  events: readonly TimelineEvent[]
  legs: readonly LegRecord[]
  timeZone: string
}) {
  const seqs = timelineLegs(events)
  const kindOf = (seq: number) => legs.find((l) => l.seq === seq)?.kind
  return (
    <section aria-labelledby="tl-h">
      <h3 id="tl-h" className="mb-2 text-sm font-semibold text-fg">
        Timeline
      </h3>
      <ol className="relative ml-1 space-y-2 border-l border-line pl-4">
        {events.map((e, i) => {
          const seq = seqs[i] ?? 0
          const kind = kindOf(seq)
          const newLeg = e.type === 'leg_started' // RESPONSE starts with the dispatch (no leg_started)
          return (
            <li key={e.event_id} className="relative">
              {newLeg && kind ? (
                <p className="-ml-4 mb-1 border-t border-line pl-4 pt-2 text-xs font-medium text-fg">
                  {LEG_LABEL[kind]} leg
                </p>
              ) : null}
              <span
                aria-hidden="true"
                className={`absolute -left-[21px] ${newLeg && kind ? 'top-9' : 'top-1.5'} h-2.5 w-2.5 rounded-full border-2 border-surface ${TIMELINE_DOT[e.type] ?? 'bg-accent'}`}
              />
              <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
                <span className="text-xs tabular-nums text-muted">
                  {formatClock(e.ts, timeZone)}
                </span>
                <span className="text-fg">{TIMELINE_LABEL[e.type] ?? e.type}</span>
                <span className="text-xs text-muted">{payloadSummary(e, kind)}</span>
              </div>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
