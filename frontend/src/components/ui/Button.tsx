import clsx from 'clsx'
import type { ButtonHTMLAttributes, ReactNode } from 'react'

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'
type Size = 'sm' | 'md'

const VARIANT: Record<Variant, string> = {
  primary:
    'bg-rose-600 text-white hover:bg-rose-500 focus-visible:ring-rose-400 disabled:bg-rose-900/60',
  secondary:
    'bg-ink-700 text-slate-100 ring-1 ring-inset ring-white/10 hover:bg-ink-600 focus-visible:ring-sky-400',
  ghost: 'text-slate-300 hover:bg-white/5 hover:text-white focus-visible:ring-sky-400',
  danger:
    'bg-transparent text-red-300 ring-1 ring-inset ring-red-500/40 hover:bg-red-500/10 focus-visible:ring-red-400',
}

const SIZE: Record<Size, string> = {
  sm: 'h-7 px-2.5 text-xs gap-1.5',
  md: 'h-9 px-3.5 text-sm gap-2',
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  icon?: ReactNode
}

export function Button({
  variant = 'secondary',
  size = 'md',
  icon,
  className,
  children,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={clsx(
        'inline-flex select-none items-center justify-center rounded-md font-medium transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:ring-offset-ink-900',
        'disabled:cursor-not-allowed disabled:opacity-60',
        VARIANT[variant],
        SIZE[size],
        className,
      )}
      {...rest}
    >
      {icon}
      {children}
    </button>
  )
}
