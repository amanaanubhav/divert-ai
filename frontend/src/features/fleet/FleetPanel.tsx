import clsx from 'clsx'
import { useMemo } from 'react'
import type { CityDetail, Station } from '../../api/types'
import { ServiceDot } from '../../components/ui/Badges'
import { EmptyState, Panel } from '../../components/ui/Panel'
import {
  formatDistance,
  formatDuration,
  formatSeconds,
  formatSpeed,
  SERVICE_LABEL,
} from '../../lib/format'
import { nextSignal, useLiveStore, type LiveVehicle } from '../../store/live'
import { useUiStore } from '../../store/ui'

export function FleetPanel({ city }: { city: CityDetail }) {
  const vehicles = useLiveStore((s) => s.vehicles)
  const signals = useLiveStore((s) => s.signals)
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
              claimState={claimState(v, signals)}
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

function claimState(
  v: LiveVehicle,
  signals: ReturnType<typeof useLiveStore.getState>['signals'],
): 'PREEMPTED' | 'CONFLICT' | 'WAITING' | null {
  const next = nextSignal(v)
  if (!next) return null
  const s = signals[next.entry.signal_id]
  if (!s) return 'WAITING'
  const mine = s.claimants.find((c) => c.vehicle_id === v.id)
  if (!mine) return 'WAITING'
  return mine.state
}

function FleetRow({
  v,
  station,
  claimState,
  savedS,
  selected,
  onSelect,
}: {
  v: LiveVehicle
  station: Station | undefined
  claimState: 'PREEMPTED' | 'CONFLICT' | 'WAITING' | null
  savedS: number
  selected: boolean
  onSelect: () => void
}) {
  const next = nextSignal(v)
  const progress = v.distance_m > 0 ? Math.min(1, v.along_m / v.distance_m) : 0
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={selected}
        className={clsx(
          'focus-inset w-full border-l-[3px] px-3 py-2.5 text-left',
          selected ? 'border-l-accent bg-primary-subtle' : 'border-l-transparent hover:bg-subtle',
        )}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="flex min-w-0 items-center gap-2">
            <ServiceDot service={v.service} />
            <span className="truncate font-mono text-sm font-semibold text-fg">{v.id}</span>
          </span>
          {v.status === 'ON_SCENE' ? (
            <span className="text-xs font-medium text-warn">On scene</span>
          ) : (
            <span
              className="text-sm font-semibold tabular-nums text-fg"
              aria-label={`ETA ${formatDuration(v.eta_s)}`}
            >
              {formatDuration(v.eta_s)}
            </span>
          )}
        </div>
        <div className="mt-0.5 truncate text-xs text-muted">
          {stationLabel(station, v.station_id)}
        </div>
        <div
          className="mt-1.5 h-1.5 overflow-hidden rounded-sm bg-subtle"
          role="progressbar"
          aria-label="Route progress"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(progress * 100)}
        >
          <div className="h-full bg-accent" style={{ width: `${progress * 100}%` }} />
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted">
          <span>{formatSpeed(v.speed_mps)}</span>
          <span>{formatDistance(Math.max(0, v.distance_m - v.along_m))} left</span>
          {next ? (
            <span className="inline-flex items-center gap-1">
              <span
                aria-hidden="true"
                className={clsx(
                  'h-1.5 w-1.5 rounded-full',
                  claimState === 'PREEMPTED' && 'bg-ok',
                  claimState === 'CONFLICT' && 'rotate-45 rounded-none bg-warn',
                  claimState === 'WAITING' && 'bg-muted',
                )}
              />
              next {next.entry.name ?? next.entry.signal_id} in {formatDistance(next.distance_m)} ·
              lane {next.entry.lane}
              {claimState && claimState !== 'WAITING' ? (
                <span className={claimState === 'CONFLICT' ? 'text-warn' : 'text-ok'}>
                  {' '}
                  {claimState.toLowerCase()}
                </span>
              ) : null}
            </span>
          ) : v.status === 'EN_ROUTE' ? (
            <span>no signals ahead</span>
          ) : null}
          <span title="Estimate: pre-empted junctions passed × city average junction delay">
            saved ≈ <span className="tabular-nums text-ok">{formatSeconds(savedS)}</span>
          </span>
        </div>
      </button>
    </li>
  )
}
