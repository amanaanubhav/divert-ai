import clsx from 'clsx'
import type { ComponentPropsWithRef, ReactNode } from 'react'

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'
type Size = 'sm' | 'md'

const VARIANT: Record<Variant, string> = {
  primary:
    'border-primary bg-primary text-on-primary hover:border-primary-hover hover:bg-primary-hover active:bg-primary-active',
  secondary: 'border-field/60 bg-surface text-fg hover:bg-subtle active:bg-line/60',
  ghost: 'border-transparent bg-transparent text-fg hover:bg-subtle active:bg-line/60',
  danger: 'border-bad/50 bg-surface text-bad hover:bg-bad-subtle',
}

const SIZE: Record<Size, string> = {
  sm: 'h-7 px-2.5 text-xs gap-1.5',
  md: 'h-8 px-3 text-sm gap-2',
}

export interface ButtonProps extends ComponentPropsWithRef<'button'> {
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
        'inline-flex select-none items-center justify-center whitespace-nowrap rounded border font-medium',
        'disabled:cursor-not-allowed disabled:opacity-55',
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
