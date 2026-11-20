import { useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { config, wsUrl } from '../config'
import { useLiveStore, vehiclesMissingRoute } from '../store/live'
import { api } from './client'
import { queryKeys } from './queries'
import { ReconnectingStream } from './stream'

const ROUTE_REFETCH_COOLDOWN_MS = 5_000
const INVALIDATE_DEBOUNCE_MS = 700

/**
 * Connects the live store to `/api/v1/cities/{city}/stream` while mounted. Incident status
 * changes refresh the REST views (incidents, unit availability, analytics) debounced, and
 * vehicles that arrive without a route (e.g. after a resync) get it from `/vehicles`.
 */
export function useCityStream(city: string): void {
  const qc = useQueryClient()

  useEffect(() => {
    const live = useLiveStore.getState()
    live.reset(city)

    let invalidateTimer: ReturnType<typeof setTimeout> | null = null
    const scheduleInvalidate = () => {
      if (invalidateTimer !== null) return
      invalidateTimer = setTimeout(() => {
        invalidateTimer = null
        void qc.invalidateQueries({ queryKey: queryKeys.incidents(city) })
        void qc.invalidateQueries({ queryKey: queryKeys.city(city) })
        void qc.invalidateQueries({ queryKey: ['analytics', city] })
      }, INVALIDATE_DEBOUNCE_MS)
    }

    let lastRouteFetch = 0
    let routeFetchTimer: ReturnType<typeof setTimeout> | null = null
    const ensureRoutes = () => {
      if (routeFetchTimer !== null) return
      if (vehiclesMissingRoute(useLiveStore.getState()).length === 0) return
      const wait = Math.max(1_000, lastRouteFetch + ROUTE_REFETCH_COOLDOWN_MS - Date.now())
      routeFetchTimer = setTimeout(() => {
        lastRouteFetch = Date.now()
        api
          .vehicles(city, true)
          .then((r) => useLiveStore.getState().mergeRoutes(r.vehicles))
          .catch(() => undefined)
          .finally(() => {
            routeFetchTimer = null
          })
      }, wait)
    }

    const stream = new ReconnectingStream({
      url: wsUrl(`/api/v1/cities/${encodeURIComponent(city)}/stream`),
      initialDelayMs: config.ws.initialDelayMs,
      maxDelayMs: config.ws.maxDelayMs,
      pingIntervalMs: config.ws.pingIntervalMs,
      onMessage: (msg) => {
        useLiveStore.getState().apply(msg)
        if (msg.type === 'incident_updated' || msg.type === 'vehicle_removed') scheduleInvalidate()
        if (msg.type === 'tick' || msg.type === 'snapshot') ensureRoutes()
      },
      onStatus: (info) => useLiveStore.getState().setConnection(info),
    })
    stream.start()

    return () => {
      stream.stop()
      if (invalidateTimer !== null) clearTimeout(invalidateTimer)
      if (routeFetchTimer !== null) clearTimeout(routeFetchTimer)
    }
  }, [city, qc])
}
