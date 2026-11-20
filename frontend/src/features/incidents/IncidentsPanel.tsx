import clsx from 'clsx'
import { useState } from 'react'
import { ApiError, describeError } from '../../api/client'
import { useCancelIncident } from '../../api/queries'
import type { CityDetail, Incident } from '../../api/types'
import { StatusBadge, TypeBadge } from '../../components/ui/Badges'
import { Button } from '../../components/ui/Button'
import { EmptyState, Panel, Spinner } from '../../components/ui/Panel'
import { toast } from '../../components/ui/toast'
import { formatClock } from '../../lib/format'
import { useLiveStore } from '../../store/live'
import { useUiStore } from '../../store/ui'
import { isActive } from './merge'

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
  const selectVehicle = useUiStore((s) => s.selectVehicle)
  const cancel = useCancelIncident(city.id)
  const [confirming, setConfirming] = useState<string | null>(null)

  const vehicleFor = (incidentId: string) =>
    Object.values(vehicles).find((v) => v.incident_id === incidentId)?.id ?? null

  const doCancel = (inc: Incident) => {
    setConfirming(null)
    cancel.mutate(inc.id, {
      onSuccess: () =>
        toast.info('Incident cancelled', 'The unit is being recalled to its station.'),
      onError: (err) =>
        toast.error(
          'Cancel failed',
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
      {isLoading ? (
        <div className="p-3">
          <Spinner label="Loading incidents" />
        </div>
      ) : incidents.length === 0 ? (
        <EmptyState>No incidents reported yet.</EmptyState>
      ) : (
        <ul className="divide-y divide-white/[0.05]">
          {incidents.map((inc) => {
            const active = isActive(inc.status)
            const vid = vehicleFor(inc.id)
            return (
              <li
                key={inc.id}
                className={clsx('flex items-start gap-2 px-3 py-2', !active && 'opacity-60')}
              >
                <button
                  type="button"
                  className="min-w-0 flex-1 rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
                  onClick={() => {
                    focusOn(inc.location)
                    if (vid) selectVehicle(vid)
                  }}
                  aria-label={`Show ${inc.type.toLowerCase()} incident reported at ${formatClock(inc.reported_at, city.timezone)} on the map`}
                >
                  <div className="flex items-center gap-2">
                    <TypeBadge type={inc.type} />
                    <StatusBadge status={inc.status} />
                  </div>
                  <div className="mt-1 flex items-center gap-2 text-[11px] text-slate-500">
                    <span className="font-mono">{formatClock(inc.reported_at, city.timezone)}</span>
                    {vid ? <span className="truncate font-mono text-slate-400">{vid}</span> : null}
                  </div>
                  {inc.description ? (
                    <div className="mt-0.5 truncate text-xs text-slate-400">{inc.description}</div>
                  ) : null}
                </button>
                {active ? (
                  confirming === inc.id ? (
                    <div className="flex shrink-0 gap-1">
                      <Button size="sm" variant="danger" onClick={() => doCancel(inc)}>
                        Confirm
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirming(null)}>
                        Keep
                      </Button>
                    </div>
                  ) : (
                    <Button
                      size="sm"
                      variant="ghost"
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
