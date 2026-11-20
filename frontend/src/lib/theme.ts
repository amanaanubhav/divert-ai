import type { IncidentStatus, IncidentType, ServiceType } from '../api/types'

/**
 * One colour per service, used consistently on the map, in panels and in charts.
 * Chosen to stay distinguishable on both CARTO light and dark tiles and for the common
 * colour-vision deficiencies (blue / orange / violet), with shape + label as the second cue.
 */
export const SERVICE_COLOR: Readonly<Record<ServiceType, string>> = {
  AMBULANCE: '#2563eb',
  FIRE_ENGINE: '#ea580c',
  POLICE: '#7c3aed',
}

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

export const SIGNAL_COLOR = {
  NORMAL: '#64748b',
  PREEMPTED: '#16a34a',
  CONFLICT: '#f59e0b',
} as const

/** Tailwind classes for status badges. */
export const STATUS_TONE: Readonly<Record<IncidentStatus, string>> = {
  OPEN: 'bg-slate-500/15 text-slate-200 ring-slate-400/30',
  DISPATCHED: 'bg-sky-500/15 text-sky-200 ring-sky-400/30',
  EN_ROUTE: 'bg-rose-500/15 text-rose-200 ring-rose-400/40',
  ON_SCENE: 'bg-amber-500/15 text-amber-200 ring-amber-400/30',
  RESOLVED: 'bg-emerald-500/15 text-emerald-200 ring-emerald-400/30',
  CANCELLED: 'bg-slate-500/15 text-slate-300 ring-slate-400/20',
  FAILED: 'bg-red-600/20 text-red-200 ring-red-500/40',
}
