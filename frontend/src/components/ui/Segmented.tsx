import clsx from 'clsx'
import type { ReactNode } from 'react'

export interface SegmentOption<T extends string> {
  value: T
  label: ReactNode
  title?: string
}

/** A radio group styled as a segmented control (arrow keys move between options). */
export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
  size = 'md',
}: {
  label: string
  value: T
  options: readonly SegmentOption<T>[]
  onChange: (v: T) => void
  size?: 'sm' | 'md'
}) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className="inline-flex rounded-md bg-ink-800 p-0.5 ring-1 ring-inset ring-white/10"
    >
      {options.map((o, i) => {
        const checked = o.value === value
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={checked}
            title={o.title}
            tabIndex={checked ? 0 : -1}
            onClick={() => onChange(o.value)}
            onKeyDown={(e) => {
              const delta =
                e.key === 'ArrowRight' || e.key === 'ArrowDown'
                  ? 1
                  : e.key === 'ArrowLeft' || e.key === 'ArrowUp'
                    ? -1
                    : 0
              if (!delta) return
              e.preventDefault()
              const next = options[(i + delta + options.length) % options.length]
              if (next) onChange(next.value)
              const group = e.currentTarget.parentElement
              const btn =
                group?.querySelectorAll<HTMLButtonElement>('button[role="radio"]')[
                  (i + delta + options.length) % options.length
                ]
              btn?.focus()
            }}
            className={clsx(
              'rounded font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400',
              size === 'sm' ? 'px-2 py-0.5 text-xs' : 'px-3 py-1 text-sm',
              checked ? 'bg-rose-600 text-white shadow' : 'text-slate-300 hover:text-white',
            )}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}
