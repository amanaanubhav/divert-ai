import clsx from 'clsx'
import type { Movement } from '../../api/types'
import { MovementIcon } from './MovementIcon'

/** More lanes than this are drawn as this many (the estimate never exceeds 3 per direction). */
const MAX_DRAWN_LANES = 6

/**
 * The approach lanes of the next junction, numbered from the left kerb (India drives on the
 * left), with the target lane filled. The kerb side has a thicker edge.
 */
export function LaneDiagram({
  lanesTotal,
  target,
  movement,
}: {
  lanesTotal: number
  target: number
  movement: Movement | null
}) {
  const n = Math.max(1, Math.min(MAX_DRAWN_LANES, Math.round(lanesTotal)))
  const t = Math.max(1, Math.min(n, Math.round(target)))
  return (
    <span
      role="img"
      aria-label={`Lane ${t} of ${n}, counted from the left kerb`}
      className="inline-flex h-6 items-stretch border-l-[3px] border-l-fg/60"
    >
      {Array.from({ length: n }, (_, i) => {
        const lane = i + 1
        const on = lane === t
        return (
          <span
            key={lane}
            className={clsx(
              'flex w-4 items-center justify-center border-y border-r border-field/70',
              on ? 'bg-primary text-on-primary' : 'bg-subtle text-transparent',
            )}
          >
            {on && movement ? <MovementIcon movement={movement} size={13} /> : null}
          </span>
        )
      })}
    </span>
  )
}
