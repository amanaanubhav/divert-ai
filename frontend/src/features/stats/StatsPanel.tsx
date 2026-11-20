import clsx from 'clsx'
import { useMemo } from 'react'
import { useAnalytics } from '../../api/queries'
import type { CityDetail } from '../../api/types'
import { Panel, Stat } from '../../components/ui/Panel'
import { formatClock, formatSeconds } from '../../lib/format'
import { presetRange } from '../../lib/time'
import { useNow } from '../../lib/useNow'
import { useLiveStore } from '../../store/live'

/** Live counters (Redis via the stream) and the last 24 h from history (PostgreSQL). */
export function StatsPanel({
  city,
  activeIncidents,
}: {
  city: CityDetail
  activeIncidents: number
}) {
  const vehicles = useLiveStore((s) => s.vehicles)
  const signals = useLiveStore((s) => s.signals)
  const feed = useLiveStore((s) => s.feed)
  const now = useNow(60_000)
  const range = useMemo(() => presetRange('24h', now), [now])
  const day = useAnalytics(city.id, range.from, range.to)

  const { preempted, conflicts, liveSaved } = useMemo(() => {
    let p = 0
    let c = 0
    for (const s of Object.values(signals)) {
      if (s.state === 'CONFLICT') c += 1
      else p += 1
    }
    let passed = 0
    for (const v of Object.values(vehicles)) passed += v.preempted_passed ?? 0
    return { preempted: p, conflicts: c, liveSaved: passed * city.traffic.avg_junction_delay_s }
  }, [signals, vehicles, city.traffic.avg_junction_delay_s])

  const d = day.data
  const p50 = d ? medianOfTypes(d.response_time_s) : null

  return (
    <Panel id="stats" title="Operations" bodyClassName="space-y-3 p-3">
      <div className="grid grid-cols-2 gap-2">
        <Stat label="Vehicles live" value={Object.keys(vehicles).length} tone="accent" />
        <Stat label="Active incidents" value={activeIncidents} />
        <Stat label="Pre-empted" value={preempted} tone="good" />
        <Stat label="Conflicts" value={conflicts} tone={conflicts > 0 ? 'warn' : 'default'} />
        <Stat label="Saved (live)" value={formatSeconds(liveSaved)} hint="estimate" tone="good" />
        <Stat
          label="Resolved · 24 h"
          value={d ? (d.incidents.by_status.RESOLVED ?? 0) : '–'}
          hint={d ? `of ${d.incidents.total} reported` : undefined}
        />
        <Stat
          label="Typical response · 24 h"
          value={formatSeconds(p50)}
          hint="mean of per-type p50"
        />
        <Stat
          label="Saved · 24 h"
          value={d ? formatSeconds(d.est_time_saved_s.total) : '–'}
          hint="estimate"
          tone="good"
        />
      </div>
      <div>
        <h3 className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
          Activity
        </h3>
        {feed.length === 0 ? (
          <p className="text-xs text-slate-600">Waiting for events…</p>
        ) : (
          <ol className="max-h-40 space-y-1 overflow-y-auto pr-1" aria-label="Recent activity">
            {feed.map((f) => (
              <li key={f.id} className="flex gap-2 text-[11px]">
                <span className="shrink-0 font-mono text-slate-600">
                  {formatClock(f.ts, city.timezone)}
                </span>
                <span
                  className={clsx(
                    'truncate',
                    f.tone === 'good' && 'text-emerald-300',
                    f.tone === 'warn' && 'text-amber-300',
                    f.tone === 'bad' && 'text-red-300',
                    f.tone === 'info' && 'text-slate-300',
                  )}
                >
                  {f.text}
                </span>
              </li>
            ))}
          </ol>
        )}
      </div>
    </Panel>
  )
}

/** Count-weighted mean of per-type medians (a display summary, not a true pooled median). */
function medianOfTypes(
  rt: Partial<Record<string, { count: number; p50_s: number | null }>>,
): number | null {
  let n = 0
  let sum = 0
  for (const v of Object.values(rt)) {
    if (v?.p50_s == null) continue
    n += v.count
    sum += v.p50_s * v.count
  }
  return n > 0 ? sum / n : null
}
