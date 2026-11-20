import clsx from 'clsx'
import type { ReactNode } from 'react'

export function Panel({
  title,
  actions,
  children,
  className,
  bodyClassName,
  id,
}: {
  title: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
  id?: string
}) {
  const headingId = id ? `${id}-title` : undefined
  return (
    <section
      aria-labelledby={headingId}
      className={clsx(
        'flex min-h-0 flex-col rounded-lg border border-white/[0.06] bg-ink-900/90 shadow-lg shadow-black/20',
        className,
      )}
    >
      <header className="flex items-center justify-between gap-2 border-b border-white/[0.06] px-3 py-2">
        <h2
          id={headingId}
          className="text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-400"
        >
          {title}
        </h2>
        {actions}
      </header>
      <div className={clsx('min-h-0 flex-1', bodyClassName)}>{children}</div>
    </section>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="px-3 py-6 text-center text-sm text-slate-500">{children}</p>
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-sm text-slate-400">
      <span
        aria-hidden="true"
        className="h-4 w-4 animate-spin rounded-full border-2 border-slate-600 border-t-rose-400"
      />
      <span>{label}…</span>
    </span>
  )
}

export function Stat({
  label,
  value,
  hint,
  tone = 'default',
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: 'default' | 'good' | 'warn' | 'accent'
}) {
  return (
    <div className="min-w-0 rounded-md bg-ink-800/70 px-3 py-2 ring-1 ring-inset ring-white/[0.04]">
      <div className="truncate text-[10px] font-semibold uppercase tracking-wider text-slate-500">
        {label}
      </div>
      <div
        className={clsx(
          'mt-0.5 truncate font-mono text-lg font-semibold tabular-nums',
          tone === 'good' && 'text-emerald-300',
          tone === 'warn' && 'text-amber-300',
          tone === 'accent' && 'text-rose-300',
          tone === 'default' && 'text-slate-100',
        )}
      >
        {value}
      </div>
      {hint ? <div className="truncate text-[11px] text-slate-500">{hint}</div> : null}
    </div>
  )
}
