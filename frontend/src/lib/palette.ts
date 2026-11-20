/**
 * The UI palette: the single source for colours.
 *
 * - `tailwind.config.ts` turns `LIGHT` / `DARK` into CSS variables (`--c-page`, ...) and Tailwind
 *   colour tokens (`bg-page`, `text-muted`, `bg-primary`, ...). Light is the default; dark follows
 *   `prefers-color-scheme`.
 * - `lib/theme.ts` re-exports the service and signal colours for the map, badges and charts.
 * - `lib/palette.test.ts` checks contrast (WCAG AA) and colour-blind separation.
 *
 * Keep this file free of DOM and React imports: the Tailwind config (Node) imports it too.
 */

export interface UiPalette {
  /** Page background behind panels. */
  page: string
  /** Panels, header, popovers, drawer, table body. */
  surface: string
  /** Table headers, stat tiles, hover and selected-neutral backgrounds. */
  subtle: string
  /** 1 px panel borders and dividers (decorative). */
  line: string
  /** Borders of inputs and other controls (>= 3:1 against `surface`). */
  field: string
  /** Body text. */
  fg: string
  /** Secondary text: labels, hints, table headers. */
  muted: string
  /** Primary action background (buttons, selected segment). */
  primary: string
  primaryHover: string
  primaryActive: string
  /** Text on `primary`. */
  onPrimary: string
  /** Primary as text / underline / outline colour on `surface` (selected tab, links, focus). */
  accent: string
  /** Light tint of the primary for selected rows. */
  primarySubtle: string
  /** Status colours: text/icon on `surface`, and a subtle background for badges. */
  ok: string
  okSubtle: string
  warn: string
  warnSubtle: string
  bad: string
  badSubtle: string
  info: string
  infoSubtle: string
  /** Modal backdrop (used with opacity). */
  scrim: string
}

export const LIGHT: UiPalette = {
  page: '#f5f6f8',
  surface: '#ffffff',
  subtle: '#eef1f4',
  line: '#d7dbe0',
  field: '#8c959f',
  fg: '#1f2328',
  muted: '#57606a',
  primary: '#1f4e79',
  primaryHover: '#183f62',
  primaryActive: '#12304b',
  onPrimary: '#ffffff',
  accent: '#1f4e79',
  primarySubtle: '#e7eef6',
  ok: '#1e6b34',
  okSubtle: '#e5f2e8',
  warn: '#8a5a00',
  warnSubtle: '#fdf2d8',
  bad: '#b42318',
  badSubtle: '#fbe9e7',
  info: '#1f4e79',
  infoSubtle: '#e7eef6',
  scrim: '#1f2328',
}

/** Neutral charcoal (no navy, no neon). */
export const DARK: UiPalette = {
  page: '#141517',
  surface: '#1c1e21',
  subtle: '#25282c',
  line: '#33373d',
  field: '#6b737c',
  fg: '#e4e6e9',
  muted: '#a0a7af',
  primary: '#2a5f94',
  primaryHover: '#2f6aa3',
  primaryActive: '#245281',
  onPrimary: '#ffffff',
  accent: '#8ab4e0',
  primarySubtle: '#1f2d3d',
  ok: '#6cc082',
  okSubtle: '#1d2e22',
  warn: '#d9a441',
  warnSubtle: '#33291a',
  bad: '#f08c84',
  badSubtle: '#3a2120',
  info: '#8ab4e0',
  infoSubtle: '#1f2d3d',
  scrim: '#000000',
}

/**
 * One colour per emergency service (map vehicles, stations, routes, badges, charts). Based on
 * the Okabe-Ito colour-blind-safe set: blue, vermillion and a neutral slate stay apart for
 * protanopia, deuteranopia and tritanopia; a white glyph stays readable on each. Shape (glyph)
 * and a text label are always the second cue.
 */
export const SERVICE_PALETTE = {
  AMBULANCE: '#0072b2',
  FIRE_ENGINE: '#d55e00',
  POLICE: '#3d4451',
} as const

/** Junction states on the map. CONFLICT also uses a diamond marker, not colour alone. */
export const SIGNAL_PALETTE = {
  NORMAL: '#6e7781',
  PREEMPTED: '#1a7f37',
  CONFLICT: '#bf8700',
} as const

/** Other map ink (same in light and dark: map overlays sit on the tiles, not on the chrome). */
export const MAP_PALETTE = {
  /** Pending (not yet dispatched) incident pin and its preview line. */
  pending: '#1f2328',
  /** Dashed service-area outline. */
  bounds: '#1f4e79',
} as const

/** Map colours as plain CSS variables (`--map-preempted: #1a7f37`) for index.css. */
export function mapVariables(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(SIGNAL_PALETTE)) out[`--map-${k.toLowerCase()}`] = v
  for (const [k, v] of Object.entries(MAP_PALETTE)) out[`--map-${k}`] = v
  return out
}

/** `#rrggbb` -> `"r g b"` (for `rgb(var(--c-x) / <alpha>)`). */
export function hexToChannels(hex: string): string {
  const n = Number.parseInt(hex.slice(1), 16)
  return `${(n >> 16) & 255} ${(n >> 8) & 255} ${n & 255}`
}

/** `primaryHover` -> `primary-hover` (CSS variable and Tailwind token names). */
export function tokenName(key: string): string {
  return key.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)
}

/** The CSS custom properties of one palette: `{'--c-page': '245 246 248', ...}`. */
export function cssVariables(p: UiPalette): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(p) as [string, string][])
    out[`--c-${tokenName(k)}`] = hexToChannels(v)
  return out
}
