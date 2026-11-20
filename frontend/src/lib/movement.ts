import type { Movement } from '../api/types'

/**
 * Plain arrow glyphs for junction movements, as SVG path data in a 24 x 24 box (stroked, no
 * fill). The vehicle approaches from the bottom; bearings run clockwise, so negative angles
 * turn left. India drives on the left: a U-turn swings to the right (median side).
 */

/** Exit direction relative to straight ahead, in degrees (null: drawn specially). */
export const MOVEMENT_ANGLE: Readonly<Record<Movement, number | null>> = {
  STRAIGHT: 0,
  SLIGHT_LEFT: -35,
  LEFT: -90,
  SHARP_LEFT: -135,
  SLIGHT_RIGHT: 35,
  RIGHT: 90,
  SHARP_RIGHT: 135,
  U_TURN: null,
  ROUNDABOUT: null,
}

const CENTRE = 12
const STEM_FROM_Y = 21
const EXIT_LEN = 8
const HEAD_LEN = 4
const HEAD_SPREAD_DEG = 35

const fmt = (n: number) => String(Math.round(n * 100) / 100)

/** Arrow head at `tip`, for a shaft coming from `from`. */
export function arrowHead(from: readonly [number, number], tip: readonly [number, number]): string {
  const dx = tip[0] - from[0]
  const dy = tip[1] - from[1]
  const len = Math.hypot(dx, dy) || 1
  const ux = dx / len
  const uy = dy / len
  const side = (deg: number) => {
    const r = (deg * Math.PI) / 180
    // rotate the reversed direction by +/- the spread
    const bx = -ux * Math.cos(r) + uy * Math.sin(r)
    const by = -ux * Math.sin(r) - uy * Math.cos(r)
    return `${fmt(tip[0] + bx * HEAD_LEN)} ${fmt(tip[1] + by * HEAD_LEN)}`
  }
  return `M${side(HEAD_SPREAD_DEG)} L${fmt(tip[0])} ${fmt(tip[1])} L${side(-HEAD_SPREAD_DEG)}`
}

/** Path data (`d`) of the arrow for a movement. */
export function movementPath(m: Movement): string {
  if (m === 'U_TURN')
    return `M8 ${STEM_FROM_Y} V10 A4 4 0 0 1 16 10 V17 ${arrowHead([16, 12], [16, 17])}`
  if (m === 'ROUNDABOUT')
    return (
      `M${CENTRE} ${STEM_FROM_Y} V14 M${CENTRE} 10.5 m-3.5 0 a3.5 3.5 0 1 0 7 0 a3.5 3.5 0 1 0 -7 0 ` +
      `M${CENTRE} 7 V2.5 ${arrowHead([CENTRE, 7], [CENTRE, 2.5])}`
    )
  const deg = MOVEMENT_ANGLE[m] ?? 0
  const r = (deg * Math.PI) / 180
  const tip: [number, number] = [CENTRE + EXIT_LEN * Math.sin(r), CENTRE - EXIT_LEN * Math.cos(r)]
  return `M${CENTRE} ${STEM_FROM_Y} V${CENTRE} L${fmt(tip[0])} ${fmt(tip[1])} ${arrowHead([CENTRE, CENTRE], tip)}`
}
