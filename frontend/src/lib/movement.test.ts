import { describe, expect, it } from 'vitest'
import { MOVEMENTS } from '../api/types'
import { arrowHead, MOVEMENT_ANGLE, movementPath } from './movement'

describe('movement arrows', () => {
  it('draws a straight arrow up the middle with a symmetric head', () => {
    expect(movementPath('STRAIGHT')).toBe(`M12 21 V12 L12 4 ${arrowHead([12, 12], [12, 4])}`)
    const head = arrowHead([12, 12], [12, 4])
    const [a, , b] = head.split(' L')
    const ax = Number(a!.slice(1).split(' ')[0])
    const bx = Number(b!.split(' ')[0])
    expect(ax + bx).toBeCloseTo(24, 6) // mirrored around x = 12
  })

  it('turns left to the left and right to the right (bearings clockwise)', () => {
    const tipX = (m: (typeof MOVEMENTS)[number]) => {
      const seg = movementPath(m).split(' L')[1]!
      return Number(seg.split(' ')[0])
    }
    expect(tipX('LEFT')).toBeLessThan(12)
    expect(tipX('SLIGHT_LEFT')).toBeLessThan(12)
    expect(tipX('SHARP_LEFT')).toBeLessThan(12)
    expect(tipX('RIGHT')).toBeGreaterThan(12)
    expect(tipX('SLIGHT_RIGHT')).toBeGreaterThan(12)
    expect(tipX('SHARP_RIGHT')).toBeGreaterThan(12)
    expect(tipX('LEFT')).toBeCloseTo(4, 6)
    expect(tipX('RIGHT')).toBeCloseTo(20, 6)
  })

  it('has a glyph for every movement, U-turn and roundabout drawn specially', () => {
    for (const m of MOVEMENTS) expect(movementPath(m)).toMatch(/^M12 21|^M8 21/)
    expect(MOVEMENT_ANGLE.U_TURN).toBeNull()
    expect(movementPath('U_TURN')).toContain('A4 4 0 0 1 16 10') // swings right (median side)
    expect(movementPath('ROUNDABOUT')).toContain('a3.5 3.5')
  })
})
