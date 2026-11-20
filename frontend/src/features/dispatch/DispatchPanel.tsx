import { useId, useMemo, useState } from 'react'
import { ApiError, describeError } from '../../api/client'
import { useCreateIncident } from '../../api/queries'
import { INCIDENT_TYPES, SERVICE_FOR_TYPE, type CityDetail } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { Panel } from '../../components/ui/Panel'
import { Segmented } from '../../components/ui/Segmented'
import { toast } from '../../components/ui/toast'
import {
  formatDistance,
  formatDuration,
  formatLatLng,
  SERVICE_LABEL,
  TYPE_LABEL,
} from '../../lib/format'
import { SERVICE_GLYPH } from '../../lib/theme'
import { useUiStore } from '../../store/ui'
import { checkLocation, nearestStationFor } from './nearest'

const TITLE: Record<number, string> = {
  409: 'No unit available',
  422: 'Invalid incident',
  429: 'Slow down',
  503: 'Routing unavailable',
  401: 'API key required',
}

export function DispatchPanel({ city }: { city: CityDetail }) {
  const type = useUiStore((s) => s.dispatchType)
  const setType = useUiStore((s) => s.setDispatchType)
  const pending = useUiStore((s) => s.pending)
  const setPending = useUiStore((s) => s.setPending)
  const selectVehicle = useUiStore((s) => s.selectVehicle)
  const [description, setDescription] = useState('')
  const create = useCreateIncident(city.id)
  const descId = useId()

  const near = useMemo(
    () => (pending ? nearestStationFor(pending, type, city.stations) : null),
    [pending, type, city.stations],
  )

  const submit = () => {
    if (!pending || create.isPending) return
    const check = checkLocation(pending, city.bbox, city.name)
    if (!check.ok) {
      toast.warning('Outside service area', check.reason)
      return
    }
    const trimmed = description.trim()
    create.mutate(
      { type, location: pending, ...(trimmed ? { description: trimmed } : {}) },
      {
        onSuccess: (res) => {
          const v = res.vehicle
          toast.success(
            `${v.id} dispatched`,
            `ETA ${formatDuration(v.eta_s)} · ${formatDistance(v.distance_m)} · ${v.corridor.length} junction${
              v.corridor.length === 1 ? '' : 's'
            } in the green corridor`,
          )
          setPending(null)
          setDescription('')
          selectVehicle(v.id)
        },
        onError: (err) => {
          const status = err instanceof ApiError ? err.status : 0
          toast.error(
            TITLE[status] ?? 'Dispatch failed',
            describeError(err),
            err instanceof ApiError && err.requestId ? `request ${err.requestId}` : undefined,
          )
        },
      },
    )
  }

  return (
    <Panel id="dispatch" title="Dispatch" bodyClassName="space-y-3 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Segmented
          label="Incident type"
          value={type}
          onChange={setType}
          options={INCIDENT_TYPES.map((t) => ({
            value: t,
            label: (
              <span className="inline-flex items-center gap-1">
                <span aria-hidden="true">{SERVICE_GLYPH[SERVICE_FOR_TYPE[t]]}</span>
                {TYPE_LABEL[t]}
              </span>
            ),
            title: `${TYPE_LABEL[t]} → ${SERVICE_LABEL[SERVICE_FOR_TYPE[t]]}`,
          }))}
        />
      </div>

      {!pending ? (
        <p className="rounded border border-dashed border-field px-3 py-2.5 text-sm text-muted">
          Click the map inside the dashed <span className="font-medium text-fg">{city.name}</span>{' '}
          service area to place a {TYPE_LABEL[type].toLowerCase()} incident.
        </p>
      ) : (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault()
            submit()
          }}
        >
          <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="text-muted">Location</dt>
            <dd className="tabular-nums text-fg">{formatLatLng(pending)}</dd>
            <dt className="text-muted">Nearest</dt>
            <dd className="min-w-0 text-fg">
              {near ? (
                <>
                  <span className="block truncate">{near.station.name ?? near.station.id}</span>
                  <span className="text-xs text-muted">
                    {formatDistance(near.distance_m)} straight-line ·{' '}
                    {near.hasFreeUnit ? (
                      <span className="text-ok">{near.station.free_units} free</span>
                    ) : (
                      <span className="text-warn">
                        no free {SERVICE_LABEL[SERVICE_FOR_TYPE[type]].toLowerCase()} units
                      </span>
                    )}
                  </span>
                </>
              ) : (
                <span className="text-warn">
                  No {SERVICE_LABEL[SERVICE_FOR_TYPE[type]].toLowerCase()} station
                </span>
              )}
            </dd>
          </dl>
          <div>
            <label htmlFor={descId} className="mb-1 block text-sm font-medium text-fg">
              Notes <span className="font-normal text-muted">(optional)</span>
            </label>
            <textarea
              id={descId}
              value={description}
              maxLength={1000}
              rows={2}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full resize-none rounded border border-field bg-surface px-2.5 py-1.5 text-sm text-fg placeholder:text-muted"
              placeholder="e.g. 2 injured, left lane blocked"
            />
          </div>
          <div className="flex gap-2">
            <Button type="submit" variant="primary" className="flex-1" disabled={create.isPending}>
              {create.isPending
                ? 'Dispatching…'
                : `Dispatch ${SERVICE_LABEL[SERVICE_FOR_TYPE[type]].toLowerCase()}`}
            </Button>
            <Button variant="ghost" onClick={() => setPending(null)} disabled={create.isPending}>
              Clear
            </Button>
          </div>
          <p className="text-xs text-muted">
            The server picks the unit by routed ETA (OSRM + city congestion), not straight-line
            distance.
          </p>
        </form>
      )}
    </Panel>
  )
}
