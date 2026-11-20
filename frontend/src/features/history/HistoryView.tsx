import clsx from 'clsx'
import { useId, useMemo, useState } from 'react'
import { useIncidentHistory } from '../../api/queries'
import {
  INCIDENT_STATUSES,
  INCIDENT_TYPES,
  type CityDetail,
  type IncidentStatus,
  type IncidentType,
} from '../../api/types'
import { StatusBadge, TypeBadge } from '../../components/ui/Badges'
import { Button } from '../../components/ui/Button'
import { EmptyState, Panel, Spinner } from '../../components/ui/Panel'
import { Segmented } from '../../components/ui/Segmented'
import { describeError } from '../../api/client'
import {
  formatDateTime,
  formatDuration,
  formatLatLng,
  secondsBetween,
  STATUS_LABEL,
  TYPE_LABEL,
} from '../../lib/format'
import { addDays, zonedDayStartIso } from '../../lib/time'
import { IncidentDrawer } from './IncidentDrawer'

type TypeFilter = 'ALL' | IncidentType
const PAGE_SIZE = 50

/** Filterable, keyset-paginated incident history of the city (PostgreSQL). */
export function HistoryView({ city }: { city: CityDetail }) {
  const [type, setType] = useState<TypeFilter>('ALL')
  const [statuses, setStatuses] = useState<IncidentStatus[]>([])
  const [fromDay, setFromDay] = useState('')
  const [toDay, setToDay] = useState('')
  const [openId, setOpenId] = useState<string | null>(null)
  const fromId = useId()
  const toId = useId()

  const filters = useMemo(() => {
    const f: {
      type?: IncidentType[]
      status?: IncidentStatus[]
      from?: string
      to?: string
      limit: number
    } = {
      limit: PAGE_SIZE,
    }
    if (type !== 'ALL') f.type = [type]
    if (statuses.length > 0) f.status = [...statuses].sort()
    if (fromDay) f.from = zonedDayStartIso(fromDay, city.timezone)
    if (toDay) f.to = zonedDayStartIso(addDays(toDay, 1), city.timezone) // inclusive end day
    return f
  }, [type, statuses, fromDay, toDay, city.timezone])

  const q = useIncidentHistory(city.id, filters)
  const items = useMemo(() => q.data?.pages.flatMap((p) => p.items) ?? [], [q.data])
  const rangeInvalid = fromDay !== '' && toDay !== '' && toDay < fromDay

  const toggleStatus = (s: IncidentStatus) =>
    setStatuses((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]))

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 p-3">
      <Panel
        id="history-filters"
        title={`${city.name} incident history`}
        bodyClassName="flex flex-wrap items-end gap-4 p-3"
      >
        <div className="space-y-1">
          <span className="block text-[11px] font-medium text-slate-400">Type</span>
          <Segmented
            label="Type filter"
            size="sm"
            value={type}
            onChange={setType}
            options={[
              { value: 'ALL', label: 'All' },
              ...INCIDENT_TYPES.map((t) => ({ value: t, label: TYPE_LABEL[t] })),
            ]}
          />
        </div>
        <fieldset className="space-y-1">
          <legend className="mb-1 block text-[11px] font-medium text-slate-400">Status</legend>
          <div className="flex flex-wrap gap-1">
            {INCIDENT_STATUSES.map((s) => {
              const on = statuses.includes(s)
              return (
                <button
                  key={s}
                  type="button"
                  aria-pressed={on}
                  onClick={() => toggleStatus(s)}
                  className={clsx(
                    'rounded px-2 py-0.5 text-xs ring-1 ring-inset transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400',
                    on
                      ? 'bg-rose-600 text-white ring-rose-500'
                      : 'text-slate-300 ring-white/10 hover:bg-white/5',
                  )}
                >
                  {STATUS_LABEL[s]}
                </button>
              )
            })}
          </div>
        </fieldset>
        <div className="flex items-end gap-2">
          <div>
            <label htmlFor={fromId} className="block text-[11px] font-medium text-slate-400">
              From
            </label>
            <input
              id={fromId}
              type="date"
              value={fromDay}
              onChange={(e) => setFromDay(e.target.value)}
              className="mt-1 rounded-md border-0 bg-ink-800 px-2 py-1 text-xs text-slate-100 ring-1 ring-inset ring-white/10 focus:ring-2 focus:ring-sky-400"
            />
          </div>
          <div>
            <label htmlFor={toId} className="block text-[11px] font-medium text-slate-400">
              To (inclusive)
            </label>
            <input
              id={toId}
              type="date"
              value={toDay}
              onChange={(e) => setToDay(e.target.value)}
              aria-invalid={rangeInvalid}
              className="mt-1 rounded-md border-0 bg-ink-800 px-2 py-1 text-xs text-slate-100 ring-1 ring-inset ring-white/10 focus:ring-2 focus:ring-sky-400"
            />
          </div>
        </div>
        {type !== 'ALL' || statuses.length > 0 || fromDay || toDay ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setType('ALL')
              setStatuses([])
              setFromDay('')
              setToDay('')
            }}
          >
            Reset filters
          </Button>
        ) : null}
        <p className="ml-auto text-[11px] text-slate-500">Dates in {city.timezone}</p>
      </Panel>

      <Panel
        id="history-table"
        title={`Incidents · ${items.length}${q.hasNextPage ? '+' : ''}`}
        className="min-h-[20rem] flex-1"
        bodyClassName="overflow-auto"
      >
        {rangeInvalid ? (
          <EmptyState>“To” is before “From”.</EmptyState>
        ) : q.isLoading ? (
          <div className="p-4">
            <Spinner label="Loading history" />
          </div>
        ) : q.isError ? (
          <EmptyState>Could not load incidents: {describeError(q.error)}</EmptyState>
        ) : items.length === 0 ? (
          <EmptyState>No incidents match these filters.</EmptyState>
        ) : (
          <table className="w-full min-w-[44rem] text-left text-sm">
            <thead className="sticky top-0 z-10 bg-ink-900 text-[10px] uppercase tracking-wider text-slate-500">
              <tr>
                <th scope="col" className="px-3 py-2 font-medium">
                  Reported
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Type
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Status
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Response
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Location
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Notes
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  <span className="sr-only">Details</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/[0.05]">
              {items.map((inc) => (
                <tr key={inc.id} className="hover:bg-white/[0.03]">
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-xs text-slate-300">
                    {formatDateTime(inc.reported_at, city.timezone)}
                  </td>
                  <td className="px-3 py-2">
                    <TypeBadge type={inc.type} />
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={inc.status} />
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-300">
                    {formatDuration(secondsBetween(inc.reported_at, inc.on_scene_at))}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-[11px] text-slate-500">
                    {formatLatLng(inc.location)}
                  </td>
                  <td className="max-w-[16rem] truncate px-3 py-2 text-xs text-slate-400">
                    {inc.description ?? ''}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <Button size="sm" variant="secondary" onClick={() => setOpenId(inc.id)}>
                      Details
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {q.hasNextPage && !rangeInvalid ? (
          <div className="flex justify-center p-3">
            <Button onClick={() => void q.fetchNextPage()} disabled={q.isFetchingNextPage}>
              {q.isFetchingNextPage ? 'Loading…' : 'Load more'}
            </Button>
          </div>
        ) : null}
      </Panel>
      {openId ? (
        <IncidentDrawer
          incidentId={openId}
          timeZone={city.timezone}
          onClose={() => setOpenId(null)}
        />
      ) : null}
    </div>
  )
}
