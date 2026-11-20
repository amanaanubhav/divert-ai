import clsx from 'clsx'
import type { IncidentStatus, IncidentType, ServiceType } from '../../api/types'
import { SERVICE_LABEL, STATUS_LABEL, TYPE_LABEL } from '../../lib/format'
import { SERVICE_COLOR, SERVICE_GLYPH, STATUS_TONE, TYPE_COLOR } from '../../lib/theme'
import { SERVICE_FOR_TYPE } from '../../api/types'

export function StatusBadge({ status }: { status: IncidentStatus }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset',
        STATUS_TONE[status],
      )}
    >
      {status === 'EN_ROUTE' ? (
        <span
          aria-hidden="true"
          className="mr-1 h-1.5 w-1.5 animate-pulse rounded-full bg-rose-400"
        />
      ) : null}
      {STATUS_LABEL[status]}
    </span>
  )
}

export function TypeBadge({ type }: { type: IncidentType }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-medium text-slate-200">
      <span
        aria-hidden="true"
        className="inline-flex h-4 w-4 items-center justify-center rounded-sm text-[9px] text-white"
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
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-300">
      <span
        aria-hidden="true"
        className="inline-flex h-4 w-4 items-center justify-center rounded-full text-[9px] text-white"
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
