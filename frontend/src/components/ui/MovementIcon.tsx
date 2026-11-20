import clsx from 'clsx'
import type { Movement } from '../../api/types'
import { movementPath } from '../../lib/movement'

/** A plain stroked arrow for a junction movement (decorative: always next to a text label). */
export function MovementIcon({
  movement,
  className,
  size = 18,
}: {
  movement: Movement
  className?: string
  size?: number
}) {
  return (
    <svg
      aria-hidden="true"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      className={clsx('shrink-0', className)}
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={movementPath(movement)} />
    </svg>
  )
}
