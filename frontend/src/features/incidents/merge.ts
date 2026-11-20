import type { Incident, IncidentStatus } from '../../api/types'
import { ACTIVE_STATUSES } from '../../api/types'
import type { LiveIncidentStatus } from '../../store/live'

const RANK: Readonly<Record<IncidentStatus, number>> = {
  OPEN: 0,
  DISPATCHED: 1,
  EN_ROUTE: 2,
  ON_SCENE: 3,
  RESOLVED: 4,
  CANCELLED: 4,
  FAILED: 4,
}

/** Statuses only move forward; the most advanced of REST and live wins. */
export function laterStatus(a: IncidentStatus, b: IncidentStatus): IncidentStatus {
  return RANK[b] > RANK[a] ? b : a
}

/** Apply live `incident_updated` statuses on top of the (slightly stale) REST list. */
export function withLiveStatus(
  incidents: readonly Incident[],
  live: Readonly<Record<string, LiveIncidentStatus>>,
): Incident[] {
  return incidents.map((inc) => {
    const l = live[inc.id]
    if (!l) return inc
    const status = laterStatus(inc.status, l.status)
    return status === inc.status ? inc : { ...inc, status }
  })
}

export function isActive(status: IncidentStatus): boolean {
  return ACTIVE_STATUSES.includes(status)
}
