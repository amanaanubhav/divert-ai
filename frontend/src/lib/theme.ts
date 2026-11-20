import type {
  DispatchStatus,
  IncidentStatus,
  IncidentType,
  JunctionState,
  LegKind,
  ServiceType,
  UnitPhase,
} from '../api/types'
import {
  JUNCTION_PALETTE,
  LEG_PALETTE,
  MAP_PALETTE,
  SERVICE_PALETTE,
  SIGNAL_PALETTE,
} from './palette'

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

export const JUNCTION_COLOR: Readonly<Record<JunctionState, string>> = JUNCTION_PALETTE

export const MAP_COLOR = MAP_PALETTE

export const LEG_COLOR = LEG_PALETTE

/** Line colour of a leg: RESPONSE in the service colour, TRANSPORT violet, RETURN grey. */
export function legColor(kind: LegKind | undefined, service: ServiceType): string {
  if (kind === 'TRANSPORT') return LEG_PALETTE.TRANSPORT
  if (kind === 'RETURN') return LEG_PALETTE.RETURN
  return SERVICE_COLOR[service]
}

/** Dash pattern of a leg line (RETURN is dashed: it is not an emergency). */
export function legDash(kind: LegKind | undefined): string | undefined {
  return kind === 'RETURN' ? '8 7' : undefined
}

/** Neutral bar colour for charts (follows the light / dark theme). */
export const CHART_NEUTRAL = 'rgb(var(--c-muted))'

/** Tailwind classes for status badges: muted status colours, a 1 px border, no glow. */
export const STATUS_TONE: Readonly<Record<IncidentStatus, string>> = {
  OPEN: 'border-line bg-subtle text-fg',
  DISPATCHED: 'border-info/40 bg-surface text-info',
  EN_ROUTE: 'border-info/40 bg-info-subtle text-info',
  ON_SCENE: 'border-warn/40 bg-warn-subtle text-warn',
  TRANSPORTING: 'border-transport/40 bg-transport-subtle text-transport',
  AT_HOSPITAL: 'border-transport/40 bg-surface text-transport',
  RESOLVED: 'border-ok/40 bg-ok-subtle text-ok',
  CANCELLED: 'border-line bg-subtle text-muted',
  FAILED: 'border-bad/40 bg-bad-subtle text-bad',
}

/** Phase chips of a unit on a dispatch (same tones as the incident statuses). */
export const PHASE_TONE: Readonly<Record<UnitPhase, string>> = {
  DISPATCHED: STATUS_TONE.EN_ROUTE,
  ON_SCENE: STATUS_TONE.ON_SCENE,
  TRANSPORTING: STATUS_TONE.TRANSPORTING,
  AT_HOSPITAL: STATUS_TONE.AT_HOSPITAL,
  RETURNING: 'border-line bg-subtle text-muted',
}

export const DISPATCH_STATUS_TONE: Readonly<Record<DispatchStatus, string>> = {
  ...PHASE_TONE,
  COMPLETED: STATUS_TONE.RESOLVED,
  FAILED: STATUS_TONE.FAILED,
}
