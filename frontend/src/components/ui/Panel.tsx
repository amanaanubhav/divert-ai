import clsx from 'clsx'
import type { ReactNode } from 'react'

/** A titled section: white panel, 1 px border, 4 px corners. */
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
      className={clsx('flex min-h-0 flex-col rounded border border-line bg-surface', className)}
    >
      <header className="flex min-h-[2.5rem] items-center justify-between gap-2 border-b border-line px-3 py-2">
        <h2 id={headingId} className="text-sm font-semibold text-fg">
          {title}
        </h2>
        {actions}
      </header>
      <div className={clsx('min-h-0 flex-1', bodyClassName)}>{children}</div>
    </section>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="px-3 py-6 text-center text-sm text-muted">{children}</p>
}

/** Loading indicator: a static label with an ellipsis (no decorative animation). */
export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-sm text-muted">
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
    <div className="min-w-0 rounded border border-line bg-subtle/60 px-3 py-2">
      <div className="truncate text-xs text-muted">{label}</div>
      <div
        className={clsx(
          'mt-0.5 truncate text-lg font-semibold tabular-nums',
          tone === 'good' && 'text-ok',
          tone === 'warn' && 'text-warn',
          tone === 'accent' && 'text-accent',
          tone === 'default' && 'text-fg',
        )}
      >
        {value}
      </div>
      {hint ? <div className="truncate text-xs text-muted">{hint}</div> : null}
    </div>
  )
}
