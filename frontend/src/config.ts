/**
 * Runtime configuration. Everything defaults to the page's own origin: in development the
 * Vite proxy forwards `/api` (REST + WebSocket) to the backend, in production nginx does.
 *
 * - `VITE_API_BASE_URL` - absolute API origin, e.g. `https://divert.example.org` (default: same origin)
 * - `VITE_API_KEY`      - optional key sent as `X-API-Key` on POST/DELETE (not a secret once
 *                          built into a bundle; operators can also set it in the UI)
 * - `VITE_TILE_URL`, `VITE_TILE_URL_DARK`, `VITE_TILE_ATTRIBUTION` - raster basemap.
 *   Default: OpenStreetMap standard tiles (fine for development and demos under the OSMF
 *   tile usage policy; production should use a keyed provider or self-hosted tiles). With
 *   no dark URL, dark mode is the light tiles through a CSS filter.
 */

const API_KEY_STORAGE = 'divert.apiKey'

/** An env value, or `null` when unset or blank (blank build args mean "use the default"). */
function envValue(v: string | undefined): string | null {
  return v !== undefined && v.trim() !== '' ? v.trim() : null
}

function trimSlash(s: string): string {
  return s.replace(/\/+$/, '')
}

export const config = {
  apiBaseUrl: trimSlash(import.meta.env.VITE_API_BASE_URL ?? ''),
  defaultCity: 'delhi',
  /** WebSocket reconnect backoff (exponential with full jitter). */
  ws: { initialDelayMs: 500, maxDelayMs: 15_000, pingIntervalMs: 20_000 },
  tiles: {
    light:
      envValue(import.meta.env.VITE_TILE_URL) ?? 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    dark: envValue(import.meta.env.VITE_TILE_URL_DARK),
    attribution:
      envValue(import.meta.env.VITE_TILE_ATTRIBUTION) ??
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
  },
} as const

/** Absolute or same-origin URL of an API path (`/api/v1/...`). */
export function apiUrl(path: string): string {
  return `${config.apiBaseUrl}${path}`
}

/** `ws(s)://` URL of an API path, derived from the API base or the page origin. */
export function wsUrl(path: string, loc: Pick<Location, 'protocol' | 'host'> = location): string {
  const base = config.apiBaseUrl
  if (base) return `${base.replace(/^http/, 'ws')}${path}`
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${loc.host}${path}`
}

export function getApiKey(): string | null {
  try {
    const stored = localStorage.getItem(API_KEY_STORAGE)
    if (stored) return stored
  } catch {
    // storage unavailable (private mode): fall back to the build-time key
  }
  const env = import.meta.env.VITE_API_KEY
  if (!env) return null
  return env
}

export function setApiKey(key: string | null): void {
  try {
    if (key) localStorage.setItem(API_KEY_STORAGE, key)
    else localStorage.removeItem(API_KEY_STORAGE)
  } catch {
    // ignore: the key then lasts for this page only via the build-time value
  }
}
