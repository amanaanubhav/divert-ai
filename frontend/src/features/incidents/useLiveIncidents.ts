import { useMemo } from 'react'
import { useRecentIncidents } from '../../api/queries'
import type { Incident } from '../../api/types'
import { useLiveStore } from '../../store/live'
import { isActive, withLiveStatus } from './merge'

/** Recent incidents of the city (REST) with live statuses applied; active ones first. */
export function useLiveIncidents(city: string): {
  incidents: Incident[]
  active: Incident[]
  isLoading: boolean
  isError: boolean
} {
  const q = useRecentIncidents(city)
  const live = useLiveStore((s) => s.incidentStatus)
  return useMemo(() => {
    const incidents = withLiveStatus(q.data?.items ?? [], live)
    return {
      incidents,
      active: incidents.filter((i) => isActive(i.status)),
      isLoading: q.isLoading,
      isError: q.isError,
    }
  }, [q.data, q.isLoading, q.isError, live])
}
