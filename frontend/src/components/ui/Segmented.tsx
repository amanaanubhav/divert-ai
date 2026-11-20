import clsx from 'clsx'
import type { ReactNode } from 'react'

export interface SegmentOption<T extends string> {
  value: T
  label: ReactNode
  title?: string
}

/** A radio group styled as a plain button group (arrow keys move between options). */
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
      className="inline-flex max-w-full flex-wrap rounded border border-field/60 bg-surface"
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
              'whitespace-nowrap font-medium first:rounded-l-[3px] last:rounded-r-[3px]',
              i > 0 && 'border-l border-field/60',
              size === 'sm' ? 'h-7 px-2.5 text-xs' : 'h-8 px-3 text-sm',
              checked ? 'bg-primary text-on-primary' : 'text-fg hover:bg-subtle',
            )}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}
