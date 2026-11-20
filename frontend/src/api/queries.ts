import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { api } from './client'
import type { IncidentCreate, IncidentFilters, IncidentPage } from './types'

export const queryKeys = {
  ready: ['ready'] as const,
  cities: ['cities'] as const,
  city: (city: string) => ['city', city] as const,
  incidents: (city: string) => ['incidents', city] as const,
  recentIncidents: (city: string) => ['incidents', city, 'recent'] as const,
  history: (city: string, f: IncidentFilters) => ['incidents', city, 'history', f] as const,
  incident: (id: string) => ['incident', id] as const,
  track: (id: string) => ['incident', id, 'track'] as const,
  analytics: (city: string, from: string, to: string) => ['analytics', city, from, to] as const,
}

export function useReady() {
  return useQuery({
    queryKey: queryKeys.ready,
    queryFn: ({ signal }) => api.ready(signal),
    refetchInterval: 10_000,
    retry: false,
  })
}

export function useCities() {
  return useQuery({
    queryKey: queryKeys.cities,
    queryFn: ({ signal }) => api.cities(signal),
    staleTime: 60 * 60_000,
  })
}

/** Reference data of a city (stations with live free units, signals). */
export function useCity(city: string) {
  return useQuery({
    queryKey: queryKeys.city(city),
    queryFn: ({ signal }) => api.city(city, signal),
    staleTime: 30_000,
    placeholderData: (prev) => (prev?.id === city ? prev : undefined),
  })
}

export function useRecentIncidents(city: string, limit = 30) {
  return useQuery({
    queryKey: queryKeys.recentIncidents(city),
    queryFn: ({ signal }) => api.incidents(city, { limit }, signal),
    refetchInterval: 30_000,
  })
}

export function useIncidentHistory(city: string, filters: Omit<IncidentFilters, 'cursor'>) {
  return useInfiniteQuery({
    queryKey: queryKeys.history(city, filters),
    queryFn: ({ pageParam, signal }) =>
      api.incidents(city, { ...filters, cursor: pageParam }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last: IncidentPage) => last.next_cursor,
    placeholderData: keepPreviousData,
  })
}

export function useIncidentDetail(id: string | null) {
  return useQuery({
    queryKey: queryKeys.incident(id ?? ''),
    queryFn: ({ signal }) => api.incident(id ?? '', signal),
    enabled: id !== null,
  })
}

export function useTrack(id: string | null) {
  return useQuery({
    queryKey: queryKeys.track(id ?? ''),
    queryFn: ({ signal }) => api.track(id ?? '', 1000, signal),
    enabled: id !== null,
  })
}

export function useAnalytics(city: string, from: string, to: string) {
  return useQuery({
    queryKey: queryKeys.analytics(city, from, to),
    queryFn: ({ signal }) => api.analytics(city, { from, to }, signal),
    placeholderData: keepPreviousData,
    staleTime: 15_000,
  })
}

export function useCreateIncident(city: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: IncidentCreate) => api.createIncident(city, body),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.incidents(city) })
      void qc.invalidateQueries({ queryKey: queryKeys.city(city) })
    },
  })
}

export function useCancelIncident(city: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (incidentId: string) => api.cancelIncident(city, incidentId),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.incidents(city) })
    },
  })
}
