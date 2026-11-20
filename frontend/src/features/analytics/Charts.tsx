import type { ReactNode } from 'react'

export interface BarDatum {
  key: string
  label: ReactNode
  value: number | null
  color: string
  /** Tooltip / accessible description of the bar. */
  title: string
  display: string
}

/**
 * Horizontal bars on one shared scale (zero baseline): label | bar | value. Values are
 * direct-labelled in text ink; colour only carries identity. Each bar has a hover title.
 */
export function BarList({
  data,
  max,
  caption,
}: {
  data: readonly BarDatum[]
  max?: number
  caption: string
}) {
  const top = max ?? Math.max(1, ...data.map((d) => d.value ?? 0))
  return (
    <figure>
      <figcaption className="sr-only">{caption}</figcaption>
      <ul className="space-y-1.5">
        {data.map((d) => (
          <li
            key={d.key}
            className="grid grid-cols-[6rem,1fr,4.5rem] items-center gap-2 text-xs"
            title={d.title}
          >
            <span className="truncate text-slate-300">{d.label}</span>
            <span className="h-3 rounded-r bg-ink-800">
              <span
                className="block h-full rounded-r-[4px]"
                style={{
                  width: `${d.value == null ? 0 : Math.max(d.value > 0 ? 1.5 : 0, (d.value / top) * 100)}%`,
                  background: d.color,
                }}
              />
            </span>
            <span className="text-right font-mono tabular-nums text-slate-200">{d.display}</span>
          </li>
        ))}
      </ul>
    </figure>
  )
}

export interface RangeDatum {
  key: string
  label: ReactNode
  color: string
  p50: number | null
  p90: number | null
  count: number
  display50: string
  display90: string
}

/**
 * p50 (solid) and p90 (light, extending past it) per category on a shared scale: the
 * spread between them is the tail of the response-time distribution.
 */
export function PercentileBars({
  data,
  caption,
}: {
  data: readonly RangeDatum[]
  caption: string
}) {
  const top = Math.max(1, ...data.map((d) => d.p90 ?? d.p50 ?? 0))
  return (
    <figure>
      <figcaption className="sr-only">{caption}</figcaption>
      <ul className="space-y-2.5">
        {data.map((d) => (
          <li
            key={d.key}
            className="grid grid-cols-[6rem,1fr,7.5rem] items-center gap-2 text-xs"
            title={`${d.count} arrivals · p50 ${d.display50} · p90 ${d.display90}`}
          >
            <span className="truncate text-slate-300">{d.label}</span>
            <span className="relative h-4 rounded-r bg-ink-800">
              <span
                className="absolute inset-y-0 left-0 rounded-r-[4px] opacity-35"
                style={{ width: `${((d.p90 ?? 0) / top) * 100}%`, background: d.color }}
              />
              <span
                className="absolute inset-y-[3px] left-0 rounded-r-[4px]"
                style={{ width: `${((d.p50 ?? 0) / top) * 100}%`, background: d.color }}
              />
            </span>
            <span className="text-right font-mono tabular-nums text-slate-200">
              {d.display50} <span className="text-slate-500">/ {d.display90}</span>
            </span>
          </li>
        ))}
      </ul>
      <div className="mt-2 flex items-center gap-4 text-[11px] text-slate-400" aria-hidden="true">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-4 rounded-sm bg-slate-300" /> p50 (median)
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-4 rounded-sm bg-slate-300/35" /> p90
        </span>
      </div>
    </figure>
  )
}
