import clsx from 'clsx'
import type { ReactNode } from 'react'

/**
 * A titled section: white panel, 1 px border, 4 px corners. `min-w-0` everywhere: a panel is
 * never wider than its column, whatever text it holds (names truncate or wrap inside it).
 * `scrollX` marks a body that scrolls sideways on purpose (a wide table on a phone); the UI
 * check (scripts/clickability.mjs) fails any other panel that overflows sideways.
 */
export function Panel({
  title,
  actions,
  children,
  className,
  bodyClassName,
  id,
  scrollX = false,
}: {
  title: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
  id?: string
  scrollX?: boolean
}) {
  const headingId = id ? `${id}-title` : undefined
  return (
    <section
      aria-labelledby={headingId}
      className={clsx(
        'flex min-h-0 min-w-0 flex-col rounded border border-line bg-surface',
        className,
      )}
    >
      <header className="flex min-h-[2.5rem] min-w-0 items-center justify-between gap-2 border-b border-line px-3 py-2">
        <h2 id={headingId} className="min-w-0 break-words text-sm font-semibold text-fg">
          {title}
        </h2>
        {actions}
      </header>
      <div
        // relative: a scrolling body contains its sr-only labels (see App.tsx)
        className={clsx('relative min-h-0 min-w-0 flex-1', bodyClassName)}
        data-scroll-x={scrollX ? '' : undefined}
      >
        {children}
      </div>
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
      <div className="truncate text-xs text-muted" title={label}>
        {label}
      </div>
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
      {hint ? (
        <div
          className="truncate text-xs text-muted"
          title={typeof hint === 'string' ? hint : undefined}
        >
          {hint}
        </div>
      ) : null}
    </div>
  )
}
