import { describe, expect, it } from 'vitest'
import {
  cssVariables,
  DARK,
  hexToChannels,
  LIGHT,
  SERVICE_PALETTE,
  SIGNAL_PALETTE,
  type UiPalette,
} from './palette'

// ------------------------------------------------------------------ colour maths
const channels = (hex: string) =>
  [1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16) / 255) as [number, number, number]
const toLinear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
const linear = (hex: string) => channels(hex).map(toLinear) as [number, number, number]
const luminance = ([r, g, b]: [number, number, number]) => 0.2126 * r + 0.7152 * g + 0.0722 * b

/** WCAG 2.x contrast ratio. */
function contrast(a: string, b: string): number {
  const la = luminance(linear(a))
  const lb = luminance(linear(b))
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

/** Machado, Oliveira & Fernandes (2009) dichromacy simulation, severity 1.0 (linear RGB). */
const CVD: Record<string, number[][]> = {
  protanopia: [
    [0.152286, 1.052583, -0.204868],
    [0.114503, 0.786281, 0.099216],
    [-0.003882, -0.048116, 1.051998],
  ],
  deuteranopia: [
    [0.367322, 0.860646, -0.227968],
    [0.280085, 0.672501, 0.047413],
    [-0.01182, 0.04294, 0.968881],
  ],
  tritanopia: [
    [1.255528, -0.076749, -0.178779],
    [-0.078411, 0.930809, 0.147602],
    [0.004733, 0.691367, 0.3039],
  ],
}

function simulate(hex: string, m: number[][] | null): [number, number, number] {
  const v = linear(hex)
  if (!m) return v
  return m.map((row) =>
    Math.min(1, Math.max(0, (row[0] ?? 0) * v[0] + (row[1] ?? 0) * v[1] + (row[2] ?? 0) * v[2])),
  ) as [number, number, number]
}

/** CIE L*a*b* (D65) of a linear RGB triple. */
function lab([r, g, b]: [number, number, number]): [number, number, number] {
  const x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
  const y = 0.2126 * r + 0.7152 * g + 0.0722 * b
  const z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
  const f = (t: number) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116)
  return [116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))]
}

/** Smallest CIE76 ΔE between any two colours, as seen with the given vision. */
function minDeltaE(colors: readonly string[], m: number[][] | null): number {
  const labs = colors.map((c) => lab(simulate(c, m)))
  let min = Infinity
  for (let i = 0; i < labs.length; i++)
    for (let j = i + 1; j < labs.length; j++) {
      const a = labs[i] as [number, number, number]
      const b = labs[j] as [number, number, number]
      min = Math.min(min, Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]))
    }
  return min
}

const VISIONS: [string, number[][] | null][] = [
  ['normal', null],
  ...Object.entries(CVD).map(([k, m]) => [k, m] as [string, number[][]]),
]

// ------------------------------------------------------------------ checks
describe.each([
  ['light', LIGHT],
  ['dark', DARK],
] as [string, UiPalette][])('%s theme contrast (WCAG AA)', (_name, p) => {
  it.each([
    ['fg on surface', p.fg, p.surface],
    ['fg on page', p.fg, p.page],
    ['fg on subtle', p.fg, p.subtle],
    ['muted on surface', p.muted, p.surface],
    ['muted on page', p.muted, p.page],
    ['muted on subtle', p.muted, p.subtle],
    ['button text on primary', p.onPrimary, p.primary],
    ['button text on primary hover', p.onPrimary, p.primaryHover],
    ['button text on primary active', p.onPrimary, p.primaryActive],
    ['accent on surface', p.accent, p.surface],
    ['accent on primary-subtle', p.accent, p.primarySubtle],
    ['ok badge', p.ok, p.okSubtle],
    ['warn badge', p.warn, p.warnSubtle],
    ['bad badge', p.bad, p.badSubtle],
    ['info badge', p.info, p.infoSubtle],
    ['ok on surface', p.ok, p.surface],
    ['warn on surface', p.warn, p.surface],
    ['bad on surface', p.bad, p.surface],
  ])('%s >= 4.5:1', (_label, fg, bg) => {
    expect(contrast(fg, bg)).toBeGreaterThanOrEqual(4.5)
  })

  it('input borders and the focus outline are >= 3:1 against the surface', () => {
    expect(contrast(p.field, p.surface)).toBeGreaterThanOrEqual(3)
    expect(contrast(p.accent, p.surface)).toBeGreaterThanOrEqual(3)
    expect(contrast(p.accent, p.page)).toBeGreaterThanOrEqual(3)
  })
})

describe('service colours', () => {
  const services = Object.values(SERVICE_PALETTE)

  it.each(Object.entries(SERVICE_PALETTE))(
    '%s carries a readable white glyph and stands out on light surfaces (>= 3:1)',
    (_service, c) => {
      expect(contrast('#ffffff', c)).toBeGreaterThanOrEqual(3)
      expect(contrast(c, LIGHT.surface)).toBeGreaterThanOrEqual(3)
      expect(contrast(c, LIGHT.page)).toBeGreaterThanOrEqual(3)
    },
  )

  it.each(VISIONS)('stay distinguishable with %s vision (min ΔE >= 20)', (_v, m) => {
    expect(minDeltaE(services, m)).toBeGreaterThanOrEqual(20)
  })
})

describe('signal state colours', () => {
  const states = Object.values(SIGNAL_PALETTE)

  it.each(VISIONS)('stay distinguishable with %s vision (min ΔE >= 20)', (_v, m) => {
    expect(minDeltaE(states, m)).toBeGreaterThanOrEqual(20)
  })

  it('labels on the marker are readable (white on pre-empted, dark on conflict)', () => {
    expect(contrast('#ffffff', SIGNAL_PALETTE.PREEMPTED)).toBeGreaterThanOrEqual(4.5)
    expect(contrast('#1f2328', SIGNAL_PALETTE.CONFLICT)).toBeGreaterThanOrEqual(4.5)
  })
})

describe('CSS variables', () => {
  it('are emitted as rgb channel triples for every palette key', () => {
    expect(hexToChannels('#1f4e79')).toBe('31 78 121')
    const vars = cssVariables(LIGHT)
    expect(vars['--c-primary-hover']).toBe(hexToChannels(LIGHT.primaryHover))
    expect(Object.keys(vars)).toHaveLength(Object.keys(LIGHT).length)
    expect(Object.keys(cssVariables(DARK))).toEqual(Object.keys(vars))
  })
})
