import type { IncidentStatus, IncidentType, ServiceType } from '../api/types'
import { MAP_PALETTE, SERVICE_PALETTE, SIGNAL_PALETTE } from './palette'

/**
 * One colour per service, used consistently on the map, in panels and in charts
 * (`lib/palette.ts`: colour-blind-safe, white glyphs readable, checked by palette.test.ts).
 * Shape (glyph) and a text label are always the second cue.
 */
export const SERVICE_COLOR: Readonly<Record<ServiceType, string>> = SERVICE_PALETTE

export const TYPE_COLOR: Readonly<Record<IncidentType, string>> = {
  MEDICAL: SERVICE_COLOR.AMBULANCE,
  FIRE: SERVICE_COLOR.FIRE_ENGINE,
  CRIME: SERVICE_COLOR.POLICE,
}

export const SERVICE_GLYPH: Readonly<Record<ServiceType, string>> = {
  AMBULANCE: '✚',
  FIRE_ENGINE: '▲',
  POLICE: '◆',
}

export const SIGNAL_COLOR = SIGNAL_PALETTE

export const MAP_COLOR = MAP_PALETTE

/** Neutral bar colour for charts (follows the light / dark theme). */
export const CHART_NEUTRAL = 'rgb(var(--c-muted))'

/** Tailwind classes for status badges: muted status colours, a 1 px border, no glow. */
export const STATUS_TONE: Readonly<Record<IncidentStatus, string>> = {
  OPEN: 'border-line bg-subtle text-fg',
  DISPATCHED: 'border-info/40 bg-surface text-info',
  EN_ROUTE: 'border-info/40 bg-info-subtle text-info',
  ON_SCENE: 'border-warn/40 bg-warn-subtle text-warn',
  RESOLVED: 'border-ok/40 bg-ok-subtle text-ok',
  CANCELLED: 'border-line bg-subtle text-muted',
  FAILED: 'border-bad/40 bg-bad-subtle text-bad',
}
