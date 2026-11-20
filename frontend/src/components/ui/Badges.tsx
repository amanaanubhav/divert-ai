import clsx from 'clsx'
import type { IncidentStatus, IncidentType, ServiceType } from '../../api/types'
import { SERVICE_LABEL, STATUS_LABEL, TYPE_LABEL } from '../../lib/format'
import { SERVICE_COLOR, SERVICE_GLYPH, STATUS_TONE, TYPE_COLOR } from '../../lib/theme'
import { SERVICE_FOR_TYPE } from '../../api/types'

export function StatusBadge({ status }: { status: IncidentStatus }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center whitespace-nowrap rounded border px-1.5 text-xs font-medium leading-5',
        STATUS_TONE[status],
      )}
    >
      {STATUS_LABEL[status]}
    </span>
  )
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
