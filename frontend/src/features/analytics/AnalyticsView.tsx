import { useQueries } from '@tanstack/react-query'
import clsx from 'clsx'
import { useMemo, useState } from 'react'
import { api, describeError } from '../../api/client'
import { queryKeys, useAnalytics, useCities } from '../../api/queries'
import {
  DISPATCH_STATUSES,
  INCIDENT_STATUSES,
  INCIDENT_TYPES,
  LEG_KINDS,
  PREEMPT_OUTCOMES,
  SERVICE_FOR_TYPE,
  type AnalyticsSummary,
  type CityDetail,
  type PreemptOutcome,
} from '../../api/types'
import { EmptyState, Panel, Spinner, Stat } from '../../components/ui/Panel'
import { Segmented } from '../../components/ui/Segmented'
import {
  DISPATCH_STATUS_LABEL,
  formatPercent,
  formatSeconds,
  LEG_LABEL,
  OUTCOME_LABEL,
  STATUS_LABEL,
  TYPE_LABEL,
} from '../../lib/format'
import { CHART_NEUTRAL, LEG_COLOR, legColor, SIGNAL_COLOR, TYPE_COLOR } from '../../lib/theme'
import { presetRange, RANGE_PRESETS, type RangePreset } from '../../lib/time'
import { useNow } from '../../lib/useNow'
import { useUiStore } from '../../store/ui'
import { BarList, PercentileBars } from './Charts'

const PRESET_LABEL: Record<RangePreset, string> = {
  '24h': '24 h',
  '7d': '7 days',
  '30d': '30 days',
  '90d': '90 days',
}

/** Per-city cards plus detailed charts for the selected city over a selectable range. */
export function AnalyticsView({ city }: { city: CityDetail }) {
  const [preset, setPreset] = useState<RangePreset>('7d')
  const now = useNow(60_000)
  const range = useMemo(() => presetRange(preset, now), [preset, now])
  const cities = useCities()
  const setCity = useUiStore((s) => s.setCity)
  const summary = useAnalytics(city.id, range.from, range.to)

  const cityList = cities.data?.cities ?? []
  const perCity = useQueries({
    queries: cityList.map((c) => ({
      queryKey: queryKeys.analytics(c.id, range.from, range.to),
      queryFn: ({ signal }: { signal: AbortSignal }) => api.analytics(c.id, range, signal),
      staleTime: 15_000,
    })),
  })

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-semibold text-fg">Analytics</h1>
        <Segmented
          label="Time range"
          size="sm"
          value={preset}
          onChange={setPreset}
          options={RANGE_PRESETS.map((p) => ({ value: p, label: PRESET_LABEL[p] }))}
        />
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        {cityList.map((c, i) => {
          const q = perCity[i]
          const d = q?.data
          const selected = c.id === city.id
          return (
            <button
              key={c.id}
              type="button"
              onClick={() => setCity(c.id)}
              aria-pressed={selected}
              className={clsx(
                'rounded border p-3 text-left',
                selected
                  ? 'border-accent bg-primary-subtle shadow-[inset_0_0_0_1px_rgb(var(--c-accent))]'
                  : 'border-line bg-surface hover:bg-subtle/60',
              )}
            >
              <div className="flex items-baseline justify-between">
                <span className="font-semibold text-fg">{c.name}</span>
                <span className="text-xs text-muted">Last {PRESET_LABEL[preset]}</span>
              </div>
              {d ? (
                <dl className="mt-2 grid grid-cols-3 gap-2 text-xs">
                  <CardStat label="Incidents" value={String(d.incidents.total)} />
                  <CardStat label="Resolved" value={String(d.incidents.by_status.RESOLVED ?? 0)} />
                  <CardStat label="Pre-emptions" value={String(d.preemptions.total)} />
                  <CardStat label="Conflicts" value={String(d.preemptions.conflicts)} />
                  <CardStat label="ETA error" value={formatPercent(d.dispatches.eta_mape)} />
                  <CardStat label="Saved (est.)" value={formatSeconds(d.est_time_saved_s.total)} />
                  <CardStat
                    label="Transports"
                    value={d.transport_time_s ? String(d.transport_time_s.count) : '–'}
                  />
                  <CardStat
                    label="To hospital p50"
                    value={formatSeconds(d.call_to_hospital_s?.p50_s)}
                  />
                  <CardStat
                    label="Re-routes"
                    value={d.reroutes != null ? String(d.reroutes) : '–'}
                  />
                </dl>
              ) : q?.isError ? (
                <p className="mt-2 text-sm text-bad">Unavailable</p>
              ) : (
                <div className="mt-2">
                  <Spinner label="Loading" />
                </div>
              )}
            </button>
          )
        })}
      </div>

      {summary.isLoading ? (
        <Spinner label={`Loading ${city.name} analytics`} />
      ) : summary.isError ? (
        <EmptyState>Could not load analytics: {describeError(summary.error)}</EmptyState>
      ) : summary.data ? (
        <CityCharts d={summary.data} cityName={city.name} />
      ) : null}
    </div>
  )
}

function CardStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="text-sm font-medium tabular-nums text-fg">{value}</dd>
    </div>
  )
}

function CityCharts({ d, cityName }: { d: AnalyticsSummary; cityName: string }) {
  const responseTypes = INCIDENT_TYPES.filter((t) => d.response_time_s[t])
  const conflictRate =
    d.preemptions.total > 0 ? d.preemptions.conflicts / d.preemptions.total : null
  return (
    <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
      <Panel
        id="an-kpi"
        title={`${cityName} at a glance`}
        className="xl:col-span-2"
        bodyClassName="grid grid-cols-2 gap-2 p-3 sm:grid-cols-3 lg:grid-cols-6"
      >
        <Stat label="Incidents" value={d.incidents.total} />
        <Stat
          label="Arrived"
          value={d.dispatches.arrived}
          hint={`of ${d.dispatches.total} dispatches`}
        />
        <Stat label="Avg predicted ETA" value={formatSeconds(d.dispatches.avg_predicted_eta_s)} />
        <Stat label="Avg actual travel" value={formatSeconds(d.dispatches.avg_actual_travel_s)} />
        <Stat
          label="ETA error (MAPE)"
          value={formatPercent(d.dispatches.eta_mape)}
          tone={d.dispatches.eta_mape != null && d.dispatches.eta_mape <= 0.1 ? 'good' : 'default'}
        />
        <Stat
          label="Time saved"
          value={formatSeconds(d.est_time_saved_s.total)}
          hint="estimate"
          tone="good"
        />
      </Panel>

      <Panel id="an-rt" title="Response time by type · p50 / p90" bodyClassName="p-3">
        {responseTypes.length === 0 ? (
          <EmptyState>No arrivals in this range.</EmptyState>
        ) : (
          <PercentileBars
            caption="Response time percentiles by incident type (report to on scene)"
            data={responseTypes.map((t) => {
              const r = d.response_time_s[t]
              return {
                key: t,
                label: TYPE_LABEL[t],
                color: TYPE_COLOR[t],
                p50: r?.p50_s ?? null,
                p90: r?.p90_s ?? null,
                count: r?.count ?? 0,
                display50: formatSeconds(r?.p50_s),
                display90: formatSeconds(r?.p90_s),
              }
            })}
          />
        )}
        <p className="mt-3 text-xs text-muted">
          Response = (dispatch − report) + simulated travel to scene; percentiles via PostgreSQL
          percentile_cont.
        </p>
      </Panel>

      <Panel id="an-type" title="Incidents by type" bodyClassName="p-3">
        <BarList
          caption="Incident count by type"
          data={INCIDENT_TYPES.map((t) => ({
            key: t,
            label: TYPE_LABEL[t],
            value: d.incidents.by_type[t] ?? 0,
            color: TYPE_COLOR[t],
            title: `${TYPE_LABEL[t]} (${SERVICE_FOR_TYPE[t].toLowerCase().replace('_', ' ')}): ${d.incidents.by_type[t] ?? 0}`,
            display: String(d.incidents.by_type[t] ?? 0),
          }))}
        />
      </Panel>

      <Panel id="an-status" title="Incidents by status" bodyClassName="p-3">
        <BarList
          caption="Incident count by status"
          data={INCIDENT_STATUSES.map((s) => ({
            key: s,
            label: STATUS_LABEL[s],
            value: d.incidents.by_status[s] ?? 0,
            color: CHART_NEUTRAL,
            title: `${STATUS_LABEL[s]}: ${d.incidents.by_status[s] ?? 0}`,
            display: String(d.incidents.by_status[s] ?? 0),
          }))}
        />
      </Panel>

      <Panel id="an-pre" title="Signal pre-emption" bodyClassName="space-y-3 p-3">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="Pre-emptions" value={d.preemptions.total} tone="good" />
          <Stat
            label="Conflicts"
            value={d.preemptions.conflicts}
            tone={d.preemptions.conflicts > 0 ? 'warn' : 'default'}
          />
          <Stat label="Conflict rate" value={formatPercent(conflictRate)} />
          <Stat label="Junctions" value={d.preemptions.distinct_signals} hint="distinct" />
        </div>
        {d.preemptions.top_signals.length > 0 ? (
          <div>
            <h3 className="mb-1.5 text-sm font-semibold text-fg">Busiest junctions</h3>
            <BarList
              caption="Most frequently pre-empted junctions"
              data={d.preemptions.top_signals.map((s) => ({
                key: s.signal_id,
                label: <span className="font-mono">{s.signal_id}</span>,
                value: s.count,
                color: SIGNAL_COLOR.PREEMPTED,
                title: `${s.signal_id}: pre-empted ${s.count} times`,
                display: String(s.count),
              }))}
            />
          </div>
        ) : null}
        <p className="text-xs text-muted">{d.est_time_saved_s.note}</p>
      </Panel>

      <PreemptionDetail d={d} />
      <TransportPanel d={d} />
      <LegAccuracyPanel d={d} />
      {d.dispatches.by_status ? (
        <Panel id="an-dstatus" title="Dispatches by status (whole trip)" bodyClassName="p-3">
          <BarList
            caption="Dispatch count by trip status"
            data={DISPATCH_STATUSES.map((s) => ({
              key: s,
              label: DISPATCH_STATUS_LABEL[s],
              value: d.dispatches.by_status?.[s] ?? 0,
              color: CHART_NEUTRAL,
              title: `${DISPATCH_STATUS_LABEL[s]}: ${d.dispatches.by_status?.[s] ?? 0}`,
              display: String(d.dispatches.by_status?.[s] ?? 0),
            }))}
          />
        </Panel>
      ) : null}
    </div>
  )
}

const OUTCOME_COLOR: Record<PreemptOutcome, string> = {
  CLEARED: SIGNAL_COLOR.PREEMPTED,
  TIMEOUT: SIGNAL_COLOR.CONFLICT,
  CANCELLED: SIGNAL_COLOR.NORMAL,
  REROUTED: SIGNAL_COLOR.NORMAL,
}

/** Outcomes, green extension vs red truncation, free-left passes and re-routes. */
function PreemptionDetail({ d }: { d: AnalyticsSummary }) {
  const outcomes = d.preemptions.outcomes
  const green = d.preemptions.green_extension
  const red = d.preemptions.red_truncation
  if (!outcomes && green == null && d.free_left_passed == null) return null
  const split = (green ?? 0) + (red ?? 0)
  return (
    <Panel id="an-pre2" title="Pre-emption outcomes" bodyClassName="space-y-3 p-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Green extended" value={green ?? '–'} hint="phase already green" />
        <Stat label="Red truncated" value={red ?? '–'} hint="made green early" />
        <Stat label="Free left passed" value={d.free_left_passed ?? '–'} hint="never claimed" />
        <Stat label="Re-routes" value={d.reroutes ?? '–'} hint="applied" />
      </div>
      {split > 0 ? (
        <figure>
          <figcaption className="mb-1 text-xs text-muted">
            Green extension vs red truncation ({formatPercent((green ?? 0) / split, 0)} extended)
          </figcaption>
          <div className="flex h-3 overflow-hidden bg-subtle" aria-hidden="true">
            <span
              style={{
                width: `${((green ?? 0) / split) * 100}%`,
                background: SIGNAL_COLOR.PREEMPTED,
              }}
            />
            <span
              className="opacity-50"
              style={{
                width: `${((red ?? 0) / split) * 100}%`,
                background: SIGNAL_COLOR.PREEMPTED,
              }}
            />
          </div>
        </figure>
      ) : null}
      {outcomes ? (
        <div>
          <h3 className="mb-1.5 text-sm font-semibold text-fg">How claims ended</h3>
          <BarList
            caption="Pre-emption claims by outcome"
            data={PREEMPT_OUTCOMES.map((o) => ({
              key: o,
              label: OUTCOME_LABEL[o],
              value: outcomes[o] ?? 0,
              color: OUTCOME_COLOR[o],
              title: `${OUTCOME_LABEL[o]}: ${outcomes[o] ?? 0}`,
              display: String(outcomes[o] ?? 0),
            }))}
          />
        </div>
      ) : null}
    </Panel>
  )
}

/** Transport and hospital times (simulated seconds) and where patients were taken. */
function TransportPanel({ d }: { d: AnalyticsSummary }) {
  if (!d.transport_time_s && !d.hospitals) return null
  const rows = [
    { key: 'transport', label: 'Transport', s: d.transport_time_s, color: LEG_COLOR.TRANSPORT },
    { key: 'call', label: 'Call to hospital', s: d.call_to_hospital_s, color: LEG_COLOR.TRANSPORT },
    {
      key: 'back',
      label: 'Handover to back in service',
      s: d.handover_to_back_in_service_s,
      color: LEG_COLOR.RETURN,
    },
  ].filter((r) => r.s && r.s.count > 0)
  const hospitals = d.hospitals ?? []
  return (
    <Panel id="an-transport" title="Patient transport · p50 / p90" bodyClassName="space-y-3 p-3">
      {rows.length === 0 ? (
        <EmptyState>No patient transports in this range.</EmptyState>
      ) : (
        <PercentileBars
          caption="Transport, call-to-hospital and handover-to-back-in-service times"
          data={rows.map((r) => ({
            key: r.key,
            label: r.label,
            color: r.color,
            p50: r.s?.p50_s ?? null,
            p90: r.s?.p90_s ?? null,
            count: r.s?.count ?? 0,
            display50: formatSeconds(r.s?.p50_s),
            display90: formatSeconds(r.s?.p90_s),
          }))}
        />
      )}
      {hospitals.length > 0 ? (
        <div>
          <h3 className="mb-1.5 text-sm font-semibold text-fg">Hospitals patients were taken to</h3>
          <BarList
            caption="Patients per destination hospital"
            data={hospitals.map((h) => ({
              key: h.station_id,
              label: h.name ?? h.station_id,
              value: h.count,
              color: LEG_COLOR.TRANSPORT,
              title: `${h.name ?? h.station_id}: ${h.count}`,
              display: String(h.count),
            }))}
          />
        </div>
      ) : null}
      <p className="text-xs text-muted">
        Simulated seconds. Transport = scene to hospital; call to hospital = (dispatch − report) +
        dispatch to arrival at the hospital. Hospitals are chosen from OSM tags only.
      </p>
    </Panel>
  )
}

/** Predicted vs actual per leg kind (the ±10 % guarantee holds on every kind). */
function LegAccuracyPanel({ d }: { d: AnalyticsSummary }) {
  if (!d.legs) return null
  const kinds = LEG_KINDS.filter((k) => (d.legs?.[k]?.count ?? 0) > 0)
  return (
    <Panel id="an-legs" title="ETA accuracy by leg" bodyClassName="space-y-2 p-3">
      {kinds.length === 0 ? (
        <EmptyState>No legs arrived in this range.</EmptyState>
      ) : (
        <BarList
          caption="Mean absolute ETA error per leg kind"
          max={0.1}
          data={kinds.map((k) => {
            const a = d.legs?.[k]
            return {
              key: k,
              label: LEG_LABEL[k],
              value: Math.min(0.1, a?.eta_mape ?? 0),
              color: legColor(k, 'AMBULANCE'),
              title: `${LEG_LABEL[k]}: ${a?.count ?? 0} legs, mean error ${formatPercent(a?.eta_mape)}, worst ${formatPercent(a?.eta_max_error)}`,
              display: `${formatPercent(a?.eta_mape)} · max ${formatPercent(a?.eta_max_error, 0)}`,
            }
          })}
        />
      )}
      <p className="text-xs text-muted">
        Mean absolute error of predicted vs simulated travel per arrived leg (scale to 10 %, the
        guarantee); re-routed legs are left out. Response legs are drawn in the ambulance colour.
      </p>
    </Panel>
  )
}
