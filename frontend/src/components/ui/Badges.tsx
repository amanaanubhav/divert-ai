import clsx from 'clsx'
import type {
  DispatchStatus,
  IncidentStatus,
  IncidentType,
  LegKind,
  ServiceType,
  UnitPhase,
} from '../../api/types'
import {
  DISPATCH_STATUS_LABEL,
  LEG_LABEL,
  PHASE_LABEL,
  SERVICE_LABEL,
  STATUS_LABEL,
  TYPE_LABEL,
} from '../../lib/format'
import {
  DISPATCH_STATUS_TONE,
  legColor,
  PHASE_TONE,
  SERVICE_COLOR,
  SERVICE_GLYPH,
  STATUS_TONE,
  TYPE_COLOR,
} from '../../lib/theme'
import { SERVICE_FOR_TYPE } from '../../api/types'

const CHIP =
  'inline-flex items-center whitespace-nowrap rounded border px-1.5 text-xs font-medium leading-5'

/** Unit status on a dispatch (Responding, On scene, Transporting, At hospital, Returning). */
export function PhaseChip({ phase }: { phase: UnitPhase }) {
  return <span className={clsx(CHIP, PHASE_TONE[phase])}>{PHASE_LABEL[phase]}</span>
}

export function DispatchStatusBadge({ status }: { status: DispatchStatus }) {
  return (
    <span className={clsx(CHIP, DISPATCH_STATUS_TONE[status])}>
      {DISPATCH_STATUS_LABEL[status]}
    </span>
  )
}

/** Leg kind with a short sample of its map line (solid service colour, violet, dashed grey). */
export function LegTag({ kind, service }: { kind: LegKind; service: ServiceType }) {
  const color = legColor(kind, service)
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-medium text-fg">
      <svg aria-hidden="true" width="18" height="6" viewBox="0 0 18 6" className="shrink-0">
        <line
          x1="1"
          y1="3"
          x2="17"
          y2="3"
          stroke={color}
          strokeWidth="3"
          strokeLinecap="round"
          strokeDasharray={kind === 'RETURN' ? '4 3' : undefined}
        />
      </svg>
      {LEG_LABEL[kind]}
    </span>
  )
}

export function StatusBadge({ status }: { status: IncidentStatus }) {
  return <span className={clsx(CHIP, STATUS_TONE[status])}>{STATUS_LABEL[status]}</span>
}

export function TypeBadge({ type }: { type: IncidentType }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-sm font-medium text-fg">
      <span
        aria-hidden="true"
        className="inline-flex h-4 w-4 items-center justify-center rounded-sm text-[10px] text-white dark:ring-1 dark:ring-white/40"
        style={{ backgroundColor: TYPE_COLOR[type] }}
      >
        {SERVICE_GLYPH[SERVICE_FOR_TYPE[type]]}
      </span>
      {TYPE_LABEL[type]}
    </span>
  )
}

export function ServiceDot({
  service,
  withLabel = false,
}: {
  service: ServiceType
  withLabel?: boolean
}) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted">
      <span
        aria-hidden="true"
        className="inline-flex h-4 w-4 items-center justify-center rounded-full text-[10px] text-white dark:ring-1 dark:ring-white/40"
        style={{ backgroundColor: SERVICE_COLOR[service] }}
      >
        {SERVICE_GLYPH[service]}
      </span>
      {withLabel ? (
        SERVICE_LABEL[service]
      ) : (
        <span className="sr-only">{SERVICE_LABEL[service]}</span>
      )}
    </span>
  )
}
