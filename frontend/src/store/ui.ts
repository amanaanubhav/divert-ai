import { create } from 'zustand'
import type { IncidentType, LatLng } from '../api/types'
import { config } from '../config'

export const TABS = ['live', 'history', 'analytics'] as const
export type Tab = (typeof TABS)[number]

export interface LayerToggles {
  signals: boolean
  stations: boolean
  routes: boolean
}

export interface UiState {
  tab: Tab
  city: string
  /** Incident type armed for dispatch: the next map click picks the location. */
  dispatchType: IncidentType
  pending: LatLng | null
  selectedVehicle: string | null
  layers: LayerToggles
  darkTiles: boolean
  /** Request to pan the live map (nonce makes repeated requests distinct). */
  focus: { pos: LatLng; zoom?: number; nonce: number } | null
  setTab: (tab: Tab) => void
  setCity: (city: string) => void
  setDispatchType: (t: IncidentType) => void
  setPending: (p: LatLng | null) => void
  selectVehicle: (id: string | null) => void
  toggleLayer: (layer: keyof LayerToggles) => void
  setDarkTiles: (dark: boolean) => void
  focusOn: (pos: LatLng, zoom?: number) => void
}

const DARK_KEY = 'divert.darkTiles'

function readDark(): boolean {
  try {
    return localStorage.getItem(DARK_KEY) === '1'
  } catch {
    return false
  }
}

/** `#/history/kolkata` -> `{tab: 'history', city: 'kolkata'}`. */
export function parseHash(hash: string): { tab: Tab | null; city: string | null } {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean)
  const tab = (TABS as readonly string[]).includes(parts[0] ?? '') ? (parts[0] as Tab) : null
  const city = parts[1] && /^[a-z][a-z0-9_-]{0,31}$/.test(parts[1]) ? parts[1] : null
  return { tab, city }
}

export function formatHash(tab: Tab, city: string): string {
  return `#/${tab}/${city}`
}

const fromHash =
  typeof location !== 'undefined' ? parseHash(location.hash) : { tab: null, city: null }

export const useUiStore = create<UiState>()((set) => ({
  tab: fromHash.tab ?? 'live',
  city: fromHash.city ?? config.defaultCity,
  dispatchType: 'MEDICAL',
  pending: null,
  selectedVehicle: null,
  layers: { signals: true, stations: true, routes: true },
  darkTiles: readDark(),
  focus: null,
  setTab: (tab) => set({ tab }),
  setCity: (city) => set({ city, pending: null, selectedVehicle: null }),
  setDispatchType: (dispatchType) => set({ dispatchType }),
  setPending: (pending) => set({ pending }),
  selectVehicle: (selectedVehicle) => set({ selectedVehicle }),
  toggleLayer: (layer) => set((s) => ({ layers: { ...s.layers, [layer]: !s.layers[layer] } })),
  setDarkTiles: (darkTiles) => {
    try {
      localStorage.setItem(DARK_KEY, darkTiles ? '1' : '0')
    } catch {
      // per-viewer convenience only
    }
    set({ darkTiles })
  },
  focusOn: (pos, zoom) =>
    set((s) => ({
      focus: { pos, ...(zoom !== undefined ? { zoom } : {}), nonce: (s.focus?.nonce ?? 0) + 1 },
    })),
}))
