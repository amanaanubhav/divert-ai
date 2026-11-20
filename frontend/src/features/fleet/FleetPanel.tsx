import clsx from 'clsx'
import { useMemo } from 'react'
import type { ActiveSignal, CityDetail, Station, UnitPhase } from '../../api/types'
import { LegTag, PhaseChip, ServiceDot } from '../../components/ui/Badges'
import { LaneDiagram } from '../../components/ui/LaneDiagram'
import { MovementIcon } from '../../components/ui/MovementIcon'
import { EmptyState, Panel } from '../../components/ui/Panel'
import {
  formatDistance,
  formatDuration,
  formatSeconds,
  formatSpeed,
  MOVEMENT_LABEL,
  SERVICE_LABEL,
} from '../../lib/format'
import {
  dwellElapsedS,
  nextJunction,
  useLiveStore,
  type LiveVehicle,
  type NextJunction,
} from '../../store/live'
import { useUiStore } from '../../store/ui'
import {
  CLAIM_TEXT,
  claimView,
  destinationText,
  laneByText,
  phaseOf,
  type ClaimView,
} from './fleetText'
import { LiveRouteWhy } from './LiveRouteWhy'

export function FleetPanel({ city }: { city: CityDetail }) {
  const vehicles = useLiveStore((s) => s.vehicles)
  const signals = useLiveStore((s) => s.signals)
  const simTs = useLiveStore((s) => s.simTs)
  const selected = useUiStore((s) => s.selectedVehicle)
  const selectVehicle = useUiStore((s) => s.selectVehicle)
  const focusOn = useUiStore((s) => s.focusOn)

  const stationsById = useMemo(() => {
    const m = new Map<string, Station>()
    for (const s of city.stations) m.set(s.id, s)
    return m
  }, [city.stations])

  const list = useMemo(
    () =>
      Object.values(vehicles).sort((a, b) =>
        a.status === b.status ? a.eta_s - b.eta_s : a.status === 'EN_ROUTE' ? -1 : 1,
      ),
    [vehicles],
  )
  const delay = city.traffic.avg_junction_delay_s

  return (
    <Panel
      id="fleet"
      title={`Fleet on the move · ${list.length}`}
      bodyClassName="max-h-[50vh] overflow-y-auto lg:max-h-none"
    >
      {list.length === 0 ? (
        <EmptyState>No vehicles on the road. Dispatch one from the panel on the left.</EmptyState>
      ) : (
        <ul className="divide-y divide-line">
          {list.map((v) => (
            <FleetRow
              key={v.id}
              v={v}
              station={v.station_id ? stationsById.get(v.station_id) : undefined}
              signals={signals}
              simTs={simTs}
              savedS={(v.preempted_passed ?? 0) * delay}
              selected={selected === v.id}
              onSelect={() => {
                selectVehicle(selected === v.id ? null : v.id)
                focusOn(v.pos)
              }}
            />
          ))}
        </ul>
      )}
    </Panel>
  )
}

function stationLabel(station: Station | undefined, id: string | undefined): string {
  if (station?.name) return station.name
  if (station)
    return `Unnamed ${SERVICE_LABEL[station.service].toLowerCase()} station (${station.osm})`
  return id ?? 'station unknown'
}

function FleetRow({
  v,
  station,
  signals,
  simTs,
  savedS,
  selected,
  onSelect,
}: {
  v: LiveVehicle
  station: Station | undefined
  signals: Readonly<Record<string, ActiveSignal>>
  simTs: string | null
  savedS: number
  selected: boolean
  onSelect: () => void
}) {
  const next = nextJunction(v)
  const driving = v.status === 'EN_ROUTE'
  const phase = phaseOf(v)
  const progress = v.distance_m > 0 ? Math.min(1, v.along_m / v.distance_m) : 0
  return (
    <li
      className={clsx(
        'border-l-[3px]',
        selected ? 'border-l-accent bg-primary-subtle' : 'border-l-transparent',
      )}
    >
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={selected}
        className={clsx(
          'focus-inset w-full px-3 pb-1 pt-2.5 text-left',
          !selected && 'hover:bg-subtle',
        )}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="flex min-w-0 items-center gap-2">
            <ServiceDot service={v.service} />
            <span className="truncate font-mono text-sm font-semibold text-fg">{v.id}</span>
          </span>
          <span className="flex shrink-0 items-center gap-2">
            <PhaseChip phase={phase} />
            {driving ? (
              <span
                className="text-sm font-semibold tabular-nums text-fg"
                aria-label={`ETA ${formatDuration(v.eta_s)}`}
              >
                {formatDuration(v.eta_s)}
              </span>
            ) : null}
          </span>
        </div>
        <div className="mt-1 flex min-w-0 items-center gap-1.5 text-xs text-muted">
          {v.leg ? <LegTag kind={v.leg.kind} service={v.service} /> : null}
          <span className="truncate">{destinationText(v)}</span>
        </div>
        <div className="mt-0.5 truncate text-xs text-muted">
          Unit of {stationLabel(station, v.station_id)}
        </div>
        {driving ? (
          <div
            className="mt-1.5 h-1.5 overflow-hidden rounded-sm bg-subtle"
            role="progressbar"
            aria-label="Leg progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress * 100)}
          >
            <div className="h-full bg-accent" style={{ width: `${progress * 100}%` }} />
          </div>
        ) : null}
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted">
          {driving ? <span>{formatSpeed(v.speed_mps)}</span> : null}
          {driving ? (
            <span>{formatDistance(Math.max(0, v.distance_m - v.along_m))} left</span>
          ) : null}
          <span title="Estimate: pre-empted junctions passed × city average junction delay">
            saved ≈ <span className="tabular-nums text-ok">{formatSeconds(savedS)}</span>
          </span>
          {v.reroutes ? <span>re-routed {v.reroutes}×</span> : null}
        </div>
      </button>
      <div className="space-y-1.5 px-3 pb-2.5 text-xs">
        <StopNote v={v} phase={phase} simTs={simTs} />
        {next ? (
          <NextMovementBlock next={next} claim={claimView(v, next, signals)} />
        ) : driving ? (
          <p className="text-muted">No signals ahead on this leg.</p>
        ) : null}
        {v.speed_advisory_mps != null ? <Advisory mps={v.speed_advisory_mps} /> : null}
        {selected ? <LiveRouteWhy v={v} /> : null}
      </div>
    </li>
  )
}

/** Stopped: planning the next leg, or dwelling on scene / at the hospital. */
function StopNote({ v, phase, simTs }: { v: LiveVehicle; phase: UnitPhase; simTs: string | null }) {
  if (v.planning)
    return (
      <p role="status" className="text-fg">
        Planning route…
        {v.plan_error ? <span className="text-warn"> retrying: {v.plan_error}</span> : null}
      </p>
    )
  if (v.status !== 'ON_SCENE') return null
  const elapsed = dwellElapsedS(v, simTs)
  const what =
    phase === 'AT_HOSPITAL' ? 'Patient handover' : phase === 'RETURNING' ? 'Stopped' : 'Scene work'
  return (
    <p className="text-fg">
      {what} <span className="tabular-nums">{formatDuration(elapsed)}</span>
      {v.dwell_target_s ? (
        <span className="text-muted"> of {formatDuration(v.dwell_target_s)} (simulated)</span>
      ) : null}
    </p>
  )
}

function NextMovementBlock({ next, claim }: { next: NextJunction; claim: ClaimView }) {
  const where = next.name ?? next.signal_id
  const label = next.free_left
    ? 'Free left'
    : next.movement
      ? MOVEMENT_LABEL[next.movement]
      : 'Next signal'
  const showLanes = next.lane_index != null && next.lanes_total != null
  return (
    <div className="space-y-1">
      <p className="flex items-start gap-1.5 text-fg">
        {next.movement ? (
          <MovementIcon movement={next.free_left ? 'LEFT' : next.movement} className="text-fg" />
        ) : null}
        <span className="min-w-0">
          <span className="font-medium">{label}</span> in {formatDistance(next.distance_m)} at{' '}
          <span className={next.name ? '' : 'font-mono'}>{where}</span>
          {' · '}
          <ClaimText claim={claim} />
        </span>
      </p>
      {next.text ? <p className="text-muted">{next.text}</p> : null}
      {showLanes ? (
        <div className="flex items-center gap-2">
          <LaneDiagram
            lanesTotal={next.lanes_total ?? 1}
            target={next.lane_index ?? 1}
            movement={next.free_left ? 'LEFT' : next.movement}
          />
          <span className="text-fg">{laneByText(next)}</span>
        </div>
      ) : (
        <p className="text-muted">lane {next.side}</p>
      )}
    </div>
  )
}

function ClaimText({ claim }: { claim: ClaimView }) {
  switch (claim.kind) {
    case 'free_left':
      return <span className="text-ok">slip road, no signal to claim</span>
    case 'obeys':
      return <span className="text-muted">obeys the signal (no pre-emption)</span>
    case 'waiting':
      return <span className="text-muted">not claimed yet</span>
    case 'claimed':
      return (
        <span className={claim.state === 'QUEUED' ? 'text-warn' : 'text-ok'}>
          {CLAIM_TEXT[claim.state]}
          {claim.state === 'QUEUED' && claim.queuePos ? ` (position ${claim.queuePos})` : ''}
        </span>
      )
  }
}

function Advisory({ mps }: { mps: number }) {
  return (
    <p className="rounded border border-warn/40 bg-warn-subtle px-2 py-1 text-warn" role="status">
      {mps <= 0
        ? 'Stop at the stop line: another vehicle has the junction.'
        : `Slow to ${formatSpeed(mps)}: green expected on arrival.`}
    </p>
  )
}
