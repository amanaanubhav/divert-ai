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

/** Table header cell; the bottom rule is an inset shadow so it stays on the sticky header. */
const TH = 'px-3 py-2 font-medium shadow-[inset_0_-1px_0_rgb(var(--c-line))]'
const COLUMNS: { key: string; label: string; className?: string }[] = [
  { key: 'reported', label: 'Reported' },
  { key: 'type', label: 'Type' },
  { key: 'status', label: 'Status' },
  { key: 'response', label: 'Response' },
  // coordinates only where there is room; the drawer always shows the incident
  { key: 'location', label: 'Location', className: 'hidden xl:table-cell' },
  { key: 'notes', label: 'Notes' },
  { key: 'details', label: 'Details' },
]

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
    <div className="flex min-h-full flex-col gap-3 p-3 lg:h-full lg:min-h-0">
      <Panel
        id="history-filters"
        title={`${city.name} incident history`}
        bodyClassName="flex flex-wrap items-end gap-4 p-3"
      >
        <div className="space-y-1">
          <span className="block text-sm font-medium text-fg">Type</span>
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
          <legend className="mb-1 block text-sm font-medium text-fg">Status</legend>
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
                    'h-7 rounded border px-2 text-xs font-medium',
                    on
                      ? 'border-primary bg-primary text-on-primary'
                      : 'border-field/60 bg-surface text-fg hover:bg-subtle',
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
            <label htmlFor={fromId} className="block text-sm font-medium text-fg">
              From
            </label>
            <input
              id={fromId}
              type="date"
              value={fromDay}
              onChange={(e) => setFromDay(e.target.value)}
              className="mt-1 h-7 rounded border border-field bg-surface px-2 text-sm text-fg aria-[invalid=true]:border-bad"
            />
          </div>
          <div>
            <label htmlFor={toId} className="block text-sm font-medium text-fg">
              To (inclusive)
            </label>
            <input
              id={toId}
              type="date"
              value={toDay}
              onChange={(e) => setToDay(e.target.value)}
              aria-invalid={rangeInvalid}
              className="mt-1 h-7 rounded border border-field bg-surface px-2 text-sm text-fg aria-[invalid=true]:border-bad"
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
        <p className="ml-auto text-xs text-muted">Dates in {city.timezone}</p>
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
          <table className="w-full min-w-[36rem] text-left text-sm">
            <thead className="sticky top-0 z-sticky bg-subtle text-xs text-muted">
              <tr>
                {COLUMNS.map((c) => (
                  <th key={c.key} scope="col" className={clsx(TH, c.className)}>
                    {c.key === 'details' ? <span className="sr-only">{c.label}</span> : c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {items.map((inc) => (
                <tr key={inc.id} className="hover:bg-subtle/60">
                  <td className="whitespace-nowrap px-3 py-2 tabular-nums text-fg">
                    {formatDateTime(inc.reported_at, city.timezone)}
                  </td>
                  <td className="px-3 py-2">
                    <TypeBadge type={inc.type} />
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={inc.status} />
                  </td>
                  <td className="px-3 py-2 tabular-nums text-fg">
                    {formatDuration(secondsBetween(inc.reported_at, inc.on_scene_at))}
                  </td>
                  <td className="hidden whitespace-nowrap px-3 py-2 text-xs tabular-nums text-muted xl:table-cell">
                    {formatLatLng(inc.location)}
                  </td>
                  <td className="max-w-[16rem] truncate px-3 py-2 text-muted">
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
