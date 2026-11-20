import clsx from 'clsx'
import { useId, useMemo, useState } from 'react'
import { ApiError, describeError } from '../../api/client'
import { useCancelIncident } from '../../api/queries'
import { TRANSPORT_STATUSES, type CityDetail, type Incident } from '../../api/types'
import { StatusBadge, TypeBadge } from '../../components/ui/Badges'
import { Button } from '../../components/ui/Button'
import { EmptyState, Panel, Spinner } from '../../components/ui/Panel'
import { toast } from '../../components/ui/toast'
import { formatClock } from '../../lib/format'
import { useLiveStore } from '../../store/live'
import { useUiStore } from '../../store/ui'
import { CANCEL_BLOCKED_HINT, isActive } from './merge'

export function IncidentsPanel({
  city,
  incidents,
  isLoading,
}: {
  city: CityDetail
  incidents: readonly Incident[]
  isLoading: boolean
}) {
  const focusOn = useUiStore((s) => s.focusOn)
  const vehicles = useLiveStore((s) => s.vehicles)
  const liveStatus = useLiveStore((s) => s.incidentStatus)
  const selectVehicle = useUiStore((s) => s.selectVehicle)
  const cancel = useCancelIncident(city.id)
  const [confirming, setConfirming] = useState<string | null>(null)
  const hintId = useId()

  const stationNames = useMemo(
    () => new Map(city.stations.map((s) => [s.id, s.name] as const)),
    [city.stations],
  )

  const vehicleFor = (incidentId: string) =>
    Object.values(vehicles).find((v) => v.incident_id === incidentId) ?? null

  /** Destination hospital: live vehicle, live update, or the REST id against the city stations. */
  const hospitalFor = (inc: Incident): string | null => {
    const live = vehicleFor(inc.id)?.hospital ?? liveStatus[inc.id]?.hospital
    if (live) return live.name ?? live.id
    if (inc.hospital_station_id)
      return stationNames.get(inc.hospital_station_id) ?? inc.hospital_station_id
    return null
  }

  const doCancel = (inc: Incident) => {
    setConfirming(null)
    cancel.mutate(inc.id, {
      onSuccess: () =>
        toast.info('Incident cancelled', 'The unit is being recalled to its station.'),
      onError: (err) =>
        toast.error(
          err instanceof ApiError && err.code === 'cannot_cancel_transport'
            ? 'Cannot cancel'
            : 'Cancel failed',
          describeError(err),
          err instanceof ApiError && err.requestId ? `request ${err.requestId}` : undefined,
        ),
    })
  }

  const activeCount = incidents.filter((i) => isActive(i.status)).length

  return (
    <Panel
      id="incidents"
      title={`Incidents · ${activeCount} active`}
      bodyClassName="max-h-[50vh] overflow-y-auto lg:max-h-none"
    >
      <p id={hintId} className="sr-only">
        {CANCEL_BLOCKED_HINT}
      </p>
      {isLoading ? (
        <div className="p-3">
          <Spinner label="Loading incidents" />
        </div>
      ) : incidents.length === 0 ? (
        <EmptyState>No incidents reported yet.</EmptyState>
      ) : (
        <ul className="divide-y divide-line">
          {incidents.map((inc) => {
            const active = isActive(inc.status)
            const vid = vehicleFor(inc.id)?.id ?? null
            const transport = TRANSPORT_STATUSES.includes(inc.status)
            const hospital = transport ? hospitalFor(inc) : null
            return (
              <li
                key={inc.id}
                className={clsx('flex items-start gap-2 px-3 py-2', !active && 'bg-subtle/50')}
              >
                <button
                  type="button"
                  className="min-w-0 flex-1 rounded text-left"
                  onClick={() => {
                    focusOn(inc.location)
                    if (vid) selectVehicle(vid)
                  }}
                  aria-label={`Show ${inc.type.toLowerCase()} incident reported at ${formatClock(inc.reported_at, city.timezone)} on the map`}
                >
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <TypeBadge type={inc.type} />
                    <StatusBadge status={inc.status} />
                  </div>
                  <div className="mt-1 flex items-center gap-2 text-xs text-muted">
                    <span className="tabular-nums">
                      {formatClock(inc.reported_at, city.timezone)}
                    </span>
                    {vid ? <span className="truncate font-mono">{vid}</span> : null}
                  </div>
                  {hospital ? (
                    <div className="mt-0.5 truncate text-xs text-transport">to {hospital}</div>
                  ) : null}
                  {inc.description ? (
                    <div className="mt-0.5 truncate text-xs text-muted">{inc.description}</div>
                  ) : null}
                </button>
                {active ? (
                  confirming === inc.id && !transport ? (
                    <div className="flex shrink-0 gap-1">
                      <Button size="sm" variant="danger" onClick={() => doCancel(inc)}>
                        Confirm
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirming(null)}>
                        Keep
                      </Button>
                    </div>
                  ) : transport ? (
                    // aria-disabled (not disabled) so the tooltip shows and it stays focusable
                    <Button
                      size="sm"
                      variant="secondary"
                      aria-disabled="true"
                      aria-describedby={hintId}
                      title={CANCEL_BLOCKED_HINT}
                      className="cursor-not-allowed opacity-55"
                      onClick={() => toast.info('Cannot cancel', CANCEL_BLOCKED_HINT)}
                      aria-label={`Cancel ${inc.type.toLowerCase()} incident (not possible: patient on board)`}
                    >
                      Cancel
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => setConfirming(inc.id)}
                      disabled={cancel.isPending && cancel.variables === inc.id}
                      aria-label={`Cancel ${inc.type.toLowerCase()} incident`}
                    >
                      Cancel
                    </Button>
                  )
                ) : null}
              </li>
            )
          })}
        </ul>
      )}
    </Panel>
  )
}
