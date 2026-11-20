# 10 · Frontend codebase

**What this covers:** the ops console in `frontend/`:
- how its folders are organised
- how state is split between TanStack Query (REST) and zustand (live stream and UI)
- how the WebSocket client reconnects and resynchronises
- how the map draws and animates vehicles
- hash routing, theming, build-time configuration
- testing, and how the image is built and served by nginx
- recipes for common changes

The REST and WebSocket contract it consumes is in [api.md](../api.md). The live data path end to end is in [Data flows](06-data-flows.md).

Stack (`frontend/package.json`): React 19, TypeScript ~6.0 (strict), Vite 8, Tailwind 3, Leaflet 1.9 with react-leaflet 5, zustand 5, TanStack Query 5, clsx. Tests use vitest 5, Testing Library and jsdom. Node >= 22.12.

---

## 1. Annotated tree

```text
frontend/
├── package.json             scripts: dev, build (tsc -b && vite build), typecheck, lint, format(:check), test(:watch)
├── package-lock.json        locked deps (CI: npm ci)
├── index.html               SPA shell: #root, theme-color, loads /src/main.tsx
├── vite.config.ts           dev proxy (/api with WebSocket, /readyz, /healthz -> DIVERT_DEV_API), build + vitest config
├── tsconfig.json            project references -> tsconfig.app.json (src) + tsconfig.node.json (config files)
├── eslint.config.js         flat config: typescript-eslint strict type-checked, jsx-a11y, react-hooks, prettier
├── .prettierrc.json         no semicolons, single quotes, trailing commas, width 100
├── tailwind.config.ts       `ink` palette, Inter font stack, pulse-ring / slide-in animations
├── postcss.config.js        tailwindcss + autoprefixer
├── Dockerfile               node:22-alpine build -> nginx-unprivileged:1.27-alpine runtime on :8080
├── nginx/default.conf.template   nginx config rendered at start (envsubst): SPA, /api proxy, CSP
├── public/favicon.svg
└── src/
    ├── main.tsx             entry: Leaflet CSS, index.css, <StrictMode><Providers><App/></Providers></StrictMode>
    ├── index.css            Tailwind layers, dark chrome (color-scheme: dark), Leaflet and marker styles, tile filters
    ├── config.ts            build-time config (VITE_*), apiUrl(), wsUrl(), API key get/set (localStorage)
    ├── vite-env.d.ts        typing of the VITE_* variables
    ├── app/                 application shell
    │   ├── App.tsx          header (tabs, city switcher, status, key), hash routing, tab panels, one live stream
    │   ├── LiveView.tsx     Live tab layout: dispatch + stats | map | incidents + fleet
    │   ├── ApiKeyButton.tsx header dialog to set or clear the X-API-Key (stored in localStorage)
    │   ├── providers.tsx    QueryClientProvider (accepts an injected client for tests)
    │   └── queryClient.ts   makeQueryClient(): default staleTime, retry and refetch policy
    ├── api/                 talking to the backend
    │   ├── types.ts         TypeScript mirror of docs/api.md: REST bodies, stream messages, close codes
    │   ├── client.ts        request<T>(), ApiError, buildQuery(), the `api` object, describeError()
    │   ├── queries.ts       queryKeys + TanStack Query hooks (useCity, useIncidentHistory, useCreateIncident, ...)
    │   ├── stream.ts        ReconnectingStream (WebSocket with backoff + ping), parseMessage(), backoffDelay()
    │   └── useCityStream.ts hook: stream -> live store, debounced query invalidation, route back-fill
    ├── store/               zustand stores
    │   ├── live.ts          live state from the stream: vehicles (+ motion), signals, incident statuses, feed
    │   └── ui.ts            UI state: tab, city, dispatch type, pending pin, selection, layers, dark tiles, focus
    ├── lib/                 framework-free helpers
    │   ├── geo.ts           haversine geodesy (same model as backend domain/geo.py), interpolateAlong, splitAt
    │   ├── format.ts        distance / duration / speed / clock formatters, TYPE_LABEL, SERVICE_LABEL, STATUS_LABEL
    │   ├── time.ts          time-zone helpers (city-local days -> UTC instants), RANGE_PRESETS 24h/7d/30d/90d
    │   ├── theme.ts         SERVICE_COLOR, TYPE_COLOR, SERVICE_GLYPH, SIGNAL_COLOR, STATUS_TONE
    │   └── useNow.ts        Date.now() refreshed on an interval
    ├── components/ui/       shared presentational components
    │   ├── Panel.tsx        Panel (titled section), EmptyState, Spinner, Stat
    │   ├── Button.tsx       Button variants
    │   ├── Badges.tsx       StatusBadge, TypeBadge, ServiceDot
    │   ├── Segmented.tsx    radio group styled as a segmented control (arrow-key navigation)
    │   ├── toast.ts         zustand toast store + `toast.info/success/warning/error`
    │   └── Toaster.tsx      renders the toasts
    ├── features/            one folder per feature
    │   ├── city-switcher/   CitySwitcher (cities from GET /cities, fallback to CITY_IDS)
    │   ├── dispatch/        DispatchPanel (type, description, submit), DispatchMapLayer (click to place), nearest.ts (preview)
    │   ├── incidents/       IncidentsPanel (list, cancel), useLiveIncidents (REST + live statuses), merge.ts
    │   ├── fleet/           FleetPanel (live vehicles: ETA, next signal, claim state, est. time saved)
    │   ├── map/             LiveMap, LiveVehiclesLayer, ReferenceLayers, MapHelpers, icons
    │   ├── signals/         ActiveSignalsLayer (pre-empted / conflict markers), signalText.ts
    │   ├── stats/           StatsPanel (live counters + last 24 h), ConnectionStatus (stream, sim clock, /readyz)
    │   ├── history/         HistoryView (filters, keyset pages), IncidentDrawer, TrackReplay + replay.ts
    │   └── analytics/       AnalyticsView (per-city cards, charts for a range), Charts.tsx (BarList, PercentileBars)
    └── test/                test infrastructure (not shipped)
        ├── setup.ts         jest-dom matchers + Testing Library cleanup after each test
        ├── render.tsx       renderWithProviders(): fresh QueryClient (no retries) + Toaster
        └── fixtures.ts      DELHI city, incident(), ROUTE, vehicleDoc(), tick(), SUMMARY, mockFetch(), urlOf()
```

Test files sit next to the code they test (`*.test.ts` / `*.test.tsx`). See section 4.

---

## 2. Architecture

### 2.1 Folders and dependency direction

- `app/` composes features into the shell and the three tabs.
- `features/<name>/` holds one area of the UI: its components, hooks and pure helpers.
- Shared code lives in `api/` (backend I/O), `store/` (client state), `lib/` (pure helpers), `components/ui/` (presentational building blocks) and `config.ts`.

Rule: shared folders never import from `features/` or `app/`. A grep of `api/`, `store/`, `lib/` and `components/` for `../features` and `../app` finds nothing. Features may use each other's map building blocks:
- `features/map/LiveMap.tsx` composes `dispatch/DispatchMapLayer` and `signals/ActiveSignalsLayer`.
- `dispatch`, `signals` and `history` import `map/icons`.
- `history/TrackReplay` imports `map/MapHelpers`.

```mermaid
flowchart TD
    MAIN["main.tsx"] --> APP["app/ (App, LiveView, providers)"]
    APP --> FEAT["features/* (dispatch, incidents, fleet, map, signals, stats, history, analytics, city-switcher)"]
    APP --> API
    APP --> STORE
    FEAT --> API["api/ (types, client, queries, stream, useCityStream)"]
    FEAT --> STORE["store/ (live, ui)"]
    FEAT --> LIB["lib/ (geo, format, time, theme)"]
    FEAT --> UI["components/ui/"]
    API --> STORE
    API --> CFG["config.ts"]
    STORE --> LIB
    STORE --> CFG
    UI --> LIB
    API -->|"REST fetch"| BE["backend /api/v1 (via nginx or the Vite proxy)"]
    API -->|"WebSocket /stream"| BE
```

### 2.2 State management: why two libraries

| Kind of state | Lives in | Why |
|---|---|---|
| Server state from REST: city reference data, incident lists and history, incident detail and track, analytics, `/readyz` | **TanStack Query** (`api/queries.ts`) | Request/response data needs caching, de-duplication, background refetch, cancellation (`signal`), pagination (`useInfiniteQuery` with the keyset cursor) and invalidation after mutations. |
| Live state pushed by the WebSocket: vehicles with their motion, active signals, tick and sim clock, live incident statuses, activity feed, connection status | **zustand** `useLiveStore` (`store/live.ts`) | A push source, not a request. Each `snapshot` replaces the state wholesale, and `tick` arrives every 0.5 s. The map's `requestAnimationFrame` loop reads `useLiveStore.getState()` 60 times a second and subscribes imperatively (`useLiveStore.subscribe`), so React does not re-render per frame. Components select narrow slices (`useLiveStore((s) => s.vehicles)`). |
| UI state: tab, city, armed incident type, pending pin, selected vehicle, layer toggles, dark basemap, map focus requests | **zustand** `useUiStore` (`store/ui.ts`) | Shared between the map (imperative Leaflet code) and panels without prop drilling. The tab and city are mirrored to the URL hash (2.5). |
| Toasts | zustand `useToasts` (`components/ui/toast.ts`) | Fire-and-forget from anywhere. Errors stay 8 s and other tones 4.5 s. At most 5 are kept. |

How the two sides are bridged:
- **Stream to queries.** `api/useCityStream.ts` invalidates `incidents(city)`, `city(city)` (unit availability) and `['analytics', city]` when an `incident_updated` or `vehicle_removed` arrives, debounced by `INVALIDATE_DEBOUNCE_MS = 700`.
- **Queries to the stream.** Vehicles that arrive without a route (first seen in a `tick`, for example after a resync) are back-filled from `GET /vehicles?include_route=true`. The wait is at least 1 s, with a `ROUTE_REFETCH_COOLDOWN_MS = 5_000` cooldown.
- **Merging.** `features/incidents/useLiveIncidents.ts` lays the live statuses over the (slightly stale) REST list. `merge.ts` `laterStatus` lets statuses only move forward, matching the backend rule.

Query defaults (`app/queryClient.ts` `makeQueryClient`):
- `staleTime: 10_000`, `refetchOnWindowFocus: false`.
- Queries retry up to 2 times, but never on a 4xx `ApiError`. Mutations never retry.

Per-hook overrides in `api/queries.ts`:

| Hook | Override |
|---|---|
| `useReady` | refetch every 10 s, no retry |
| `useCities` | stale after 1 h |
| `useCity` | stale after 30 s, keeps the previous data only for the same city |
| `useRecentIncidents` | limit 30, refetch every 30 s |
| `useIncidentHistory` | infinite query over `next_cursor`, keeps the previous page set |
| `useTrack` | `max_points` 1000 |
| `useAnalytics` | stale after 15 s, keeps the previous data |

Keys come from the `queryKeys` factory. Mutations (`useCreateIncident`, `useCancelIncident`) invalidate the incident lists and the city in `onSettled`.

### 2.3 WebSocket client (`api/stream.ts`, `api/useCityStream.ts`)

`App` mounts `useCityStream(cityId)` once for the selected city, on every tab, so the header status and sim clock always work. The hook:
1. Calls `useLiveStore.getState().reset(city)`.
2. Opens `ReconnectingStream` on `wsUrl('/api/v1/cities/{city}/stream')`.
3. Feeds every parsed message to `useLiveStore.apply`.
4. Stops the stream on unmount or city change.

`ReconnectingStream` behaviour:

| Aspect | Behaviour (code) |
|---|---|
| Status | `idle -> connecting -> open`. Then `reconnecting` (with `attempt`, `retryInMs`), `closed` after `stop()`, or `unknown_city`. Stored in `useLiveStore.connection` and shown by `features/stats/ConnectionStatus.tsx`. |
| Backoff | `backoffDelay(attempt, 500, 15_000)`: `exp = min(max, initial x 2^attempt)`, delay = `exp/2 + random x exp/2` ("equal jitter"). The values come from `config.ws` in `config.ts`. |
| Backoff reset | `attempt` returns to 0 when a `snapshot` arrives (a healthy session), not merely on `open`. |
| Keep-alive | Sends `{"type":"ping"}` every 20 s (`pingIntervalMs`). The server answers `pong`, which the store ignores. |
| Network back | On the browser `online` event, a pending retry fires immediately. |
| Close 4404 (`WS_CLOSE_UNKNOWN_CITY`) | Stops for good, status `unknown_city`. |
| Close 1013 (`WS_CLOSE_TRY_AGAIN`) or any other close | Reconnects with backoff. The server sends 1013 for too many clients, a slow consumer, missed messages after a Redis reconnect, or live state unavailable (`backend api/v1/stream.py`, `api/ws_hub.py`). |
| Parsing | `parseMessage` drops non-strings, bad JSON, types outside `KNOWN_TYPES` (`snapshot`, `tick`, `vehicle_added`, `vehicle_removed`, `incident_updated`, `pong`) and any `v` greater than `STREAM_SCHEMA_VERSION` (1). |

**Snapshot reset.** Every successful connect starts with a `snapshot`. `apply('snapshot')` replaces `vehicles` and `signals` wholesale and clears `lastTickAt`, so a reconnect cannot leave stale vehicles behind. Messages whose `city` differs from the store's city are ignored. A `tick` carries the complete vehicle list and the complete non-NORMAL signal set: vehicles missing from it are dropped, and signals missing from it are NORMAL.

### 2.4 Map rendering (`features/map`, `features/signals`, `features/dispatch`)

`LiveMap` renders a react-leaflet `MapContainer` with `preferCanvas`, so vector layers (polylines, circle markers, rectangles) share one canvas. React renders only the structure. Everything that changes often is drawn imperatively inside `useEffect`:

| Layer | Technique | Notes |
|---|---|---|
| `BaseTiles` (`MapHelpers.tsx`) | `TileLayer` | Light tiles get the `.tiles-muted` CSS filter. Dark mode uses `VITE_TILE_URL_DARK`, or the light tiles with the `.tiles-dark` filter. |
| `FitToBBox`, `FocusController` | `useMap()` effects | Fits the city bbox and restricts panning to the bbox plus 35 %. A `ResizeObserver` refits until the user moves the map. `flyTo` handles `useUiStore.focus` requests. |
| `CityBoundsLayer`, `SignalsLayer`, `StationsLayer` (`ReferenceLayers.tsx`) | `L.rectangle` / `L.circleMarker` in a `layerGroup` on the **canvas** | No DOM node per marker, so hundreds of signals and over a thousand stations stay smooth. Radius depends on zoom. Tooltips are created lazily. Stations are hollow when no unit is free. |
| `IncidentsLayer` | react-leaflet `Marker` + `divIcon` | Active incidents only. |
| `LiveVehiclesLayer` | Imperative `L.marker` with a `divIcon` (DOM) + three canvas polylines per route (faded dashed "travelled", white casing, solid "remaining") | Re-synced only when `vehicles` changes (`useLiveStore.subscribe`). Moved by a `requestAnimationFrame` loop. |
| `ActiveSignalsLayer` | Imperative `L.marker` per active signal, updated in place each tick | Green pulsing for PREEMPTED, amber for CONFLICT, with an ETA and lane label and an approach arrow. Never re-created, so the pulse animation does not restart. |
| `DispatchMapLayer` | `useMapEvents` + react-leaflet `Marker` / `Polyline` | A click inside the bbox sets the pending pin (outside: a warning, nothing sent). The pin is draggable. A straight-line preview goes to the nearest station of the armed service (`nearest.ts`, haversine; the server ranks by routed ETA). |

**Interpolation between ticks** (`store/live.ts`, `lib/geo.ts`):
1. Each `tick` stores a `Motion` per vehicle: `fromAlong` (where it is drawn now), `toAlong` (the new `along_m`), and `durMs`.
2. `durMs` is the measured tick interval: an exponential average `0.7 x previous + 0.3 x measured`, clamped to 150..2000 ms, starting at `DEFAULT_TICK_MS = 500`.
3. Every frame, `vehiclePose(v, now)` interpolates `along` linearly in time. `interpolateAlong(route, cum, along)` turns it into a position and heading by **cumulative haversine distance** along the route. That is the same spherical model as the backend: `EARTH_RADIUS_M = 6_371_008.8` in both `lib/geo.ts` and `backend/src/divert/domain/geo.py`. The cumulative distances `cum` are computed once per route.
4. A backward jump (new `along_m` more than 1 m behind the drawn position, or a different `dispatch_id`, for example after a worker failover) snaps instead of animating.
5. The travelled/remaining split (`splitAt`) is recomputed at most every `SPLIT_EVERY_MS = 120` ms, and only after more than 1 m of movement.

The history replay (`features/history/TrackReplay.tsx` + `replay.ts`) uses the same idea on recorded samples. Between two samples of the same dispatch that have `along_m`, the vehicle follows the recorded route. Otherwise it moves in a straight line.

### 2.5 Hash routing `#/{tab}/{city}`

There is no router library. `store/ui.ts` defines:
- `TABS = ['live', 'history', 'analytics']`
- `parseHash()`: the tab must be in `TABS`; the city must match `^[a-z][a-z0-9_-]{0,31}$`
- `formatHash()`

The UI store's initial `tab` and `city` come from `location.hash` at module load. The defaults are `live` and `config.defaultCity` (`delhi`).

`useHashRoute` in `app/App.tsx` syncs both ways:
- A `hashchange` event (a link or a manual edit) updates the store.
- Store changes are written back with `history.replaceState`. So switching tabs or cities in the UI does **not** add browser history entries.

An unknown city in the hash passes `parseHash`. `GET /cities/{city}` then fails (the app shows "Could not load ..." with a Retry button), and the stream stops with status `unknown_city` (close 4404). Changing city resets the pending pin and the selection (`setCity`). `LiveView` and `HistoryView` are keyed by city id, so their local state resets.

### 2.6 Tailwind, theme and dark mode

- **The chrome is always dark.** `index.css` sets `:root { color-scheme: dark }` and `body` uses `bg-ink-950 text-slate-200`. There is no light theme and no `prefers-color-scheme` switch.
- `tailwind.config.ts` adds the `ink` palette (`950 #0b0f17` ... `600 #364157`), the `pulse-ring` (1.4 s) and `slide-in` (180 ms) animations, and a font stack starting with Inter. No font files are bundled, and the CSP only allows `font-src 'self'`, so Inter shows only where it is installed locally. Otherwise the system UI font is used.
- **"Dark mode" means the dark basemap toggle.** `useUiStore.darkTiles` is persisted in `localStorage` key `divert.darkTiles`. `TrackReplay` honours it too.
- Semantic colours live in one place, `lib/theme.ts`:
  - `SERVICE_COLOR`: ambulance `#2563eb`, fire engine `#ea580c`, police `#7c3aed`
  - `TYPE_COLOR`, `SERVICE_GLYPH`
  - `SIGNAL_COLOR`: NORMAL `#64748b`, PREEMPTED `#16a34a`, CONFLICT `#f59e0b`
  - `STATUS_TONE` (badge classes)

  Shape and label are always the second cue next to colour.

### 2.7 Configuration (`config.ts`)

All configuration is build-time (`import.meta.env.VITE_*`), plus two browser-local values (the API key and the dark-tile preference). The full table is in [Configuration](11-configuration.md) section 3.
- `apiUrl(path)` prefixes `VITE_API_BASE_URL`. Empty means same origin.
- `wsUrl(path)` derives `ws://` / `wss://` from that base, or from the page's protocol and host.
- `getApiKey()` prefers `localStorage['divert.apiKey']` (set by `ApiKeyButton`) over `VITE_API_KEY`. `request()` sends `X-API-Key` only on non-GET requests.

### 2.8 Errors

`api/client.ts` `request<T>()` turns every non-2xx answer into an `ApiError(status, code, message, details, requestId)`, using the backend's `{"error": {...}, "request_id"}` body when present. Otherwise the code is `http_<status>`. A network failure becomes `status 0`, `code 'network_error'`. An `AbortError` (query cancellation) is re-thrown as is.

`describeError()` maps known codes to operator-friendly text (`no_unit_available`, `routing_unavailable`, `invalid_incident`, `unauthorized`, `rate_limited` with `retry_after_s`, `network_error`). Toasts can show the request id for support.

---

## 3. Data flow on the Live tab

```mermaid
sequenceDiagram
    participant U as Operator
    participant DP as DispatchPanel / DispatchMapLayer
    participant Q as TanStack Query
    participant API as api (REST)
    participant WS as ReconnectingStream
    participant LS as useLiveStore
    participant MAP as LiveVehiclesLayer (rAF)
    U->>DP: pick type, click map
    DP->>Q: useCreateIncident.mutate
    Q->>API: POST /api/v1/cities/{city}/incidents
    API-->>Q: 201 incident + vehicle
    Q->>Q: invalidate incidents(city), city(city)
    WS-->>LS: vehicle_added (route, corridor)
    loop every tick (0.5 s)
        WS-->>LS: tick (all vehicles, all active signals)
        MAP->>LS: getState() every frame, vehiclePose()
    end
    WS-->>LS: incident_updated / vehicle_removed
    LS-->>Q: debounced invalidate (700 ms)
```

---

## 4. Testing

**Tooling.** vitest in `jsdom` (`vite.config.ts` `test`), with `@testing-library/react`, `@testing-library/user-event` and `@testing-library/jest-dom`.
- `src/test/setup.ts` registers the jest-dom matchers and runs `cleanup()` after each test.
- `restoreMocks` and `unstubGlobals` reset spies and stubbed globals between tests.
- Only `src/**/*.test.{ts,tsx}` files run.

**Helpers** (`src/test/`):
- `renderWithProviders(ui)`: a fresh `QueryClient` (no retries, `staleTime: Infinity`), the `Toaster`, and the client returned for assertions.
- `fixtures.ts`:
  - `DELHI` (a `CityDetail`), `incident()`, `ROUTE`, `vehicleDoc()`, `tick()`, `SUMMARY`
  - `mockFetch(routes)`, which replaces global `fetch` with a router over regexes and returns the spy
  - `urlOf()`
- Live state is seeded through the real store: `useLiveStore.getState().reset('delhi')` and then `apply({type: 'snapshot', ...})`.

| Test file | Covers |
|---|---|
| `api/client.test.ts` | `buildQuery`, JSON POST, `X-API-Key` on mutations only, history filter encoding, error body to `ApiError`, `describeError` messages |
| `api/stream.test.ts` | `parseMessage` (known types, newer `v` dropped), `backoffDelay` doubling with equal jitter, connect, forward and ping, reconnect with backoff and reset after a snapshot, no reconnect on 4404, `stop()` (with fake timers and an injected `WebSocketImpl`) |
| `store/live.test.ts` | Snapshot replaces state, other-city messages ignored, haversine interpolation between ticks, snap on a backward jump, tick replaces the signal set, `vehicle_added` / `vehicle_removed`, missing-route detection and merge, `incident_updated`, `nextSignal` |
| `lib/geo.test.ts` | Haversine (IUGG radius, backend distances), bearings, cumulative distances, interpolation and clamping, `splitAt`, projection onto the polyline, bbox and nearest item (values cross-checked against the backend) |
| `lib/format.test.ts` | Distance, duration and clock formatters (city time zone), seconds between instants, and `lib/time.ts` (zone offset, city-local midnight, a DST zone, day ranges) |
| `features/dispatch/DispatchPanel.test.tsx` | Asks for a map click, nearest-station preview, re-targeting by type, 201 selects the vehicle, out-of-bbox refused without an API call |
| `features/fleet/FleetPanel.test.tsx` | Empty state; callsign, station, ETA, next signal and claim state, estimated time saved |
| `features/history/HistoryView.test.tsx` | Keyset paging, status filter, opening the detail drawer, day filters sent as city-local midnight instants (`TrackReplay` is mocked) |
| `features/history/replay.test.ts` | `prepareReplay` / `frameAt` road-following replay |
| `features/incidents/merge.test.ts` | Forward-only status merge of REST and live data; also `parseHash` / `formatHash` round-trip (hash routing) |
| `features/analytics/AnalyticsView.test.tsx` | Per-city cards and the selected city's charts |

**Not tested:**
- **No real-browser end-to-end tests**: no Playwright or Cypress, so no test drives the built bundle against a running backend.
- Nothing that needs Leaflet's canvas: `LiveMap`, `LiveVehiclesLayer`, `ReferenceLayers`, `ActiveSignalsLayer`, `DispatchMapLayer`, `TrackReplay`. jsdom has no canvas. Their logic is covered through `store/live.ts`, `lib/geo.ts` and `replay.ts`.
- The `App` shell, including the `useHashRoute` effect (`parseHash` / `formatHash` are tested), `useCityStream` as a whole (its parts are tested), `IncidentsPanel`, `StatsPanel`, `ConnectionStatus`, `ApiKeyButton`.
- The nginx configuration, visual regressions, and accessibility beyond the `jsx-a11y` lint rules.

CI (`.github/workflows/ci.yml`, job `frontend`, Node 22) runs `npm ci`, `format:check`, `lint` (zero warnings), `typecheck`, `test` and `build`. The `docker` job then builds the image (without pushing).

---

## 5. Build and serving

### 5.1 Image (`frontend/Dockerfile`)

1. **Build stage** `node:22-alpine`:
   1. `npm ci` (npm cache mount).
   2. Copy the sources. `.dockerignore` drops `node_modules`, `dist`, `.env*` and logs.
   3. Declare `ARG VITE_API_BASE_URL`, `VITE_TILE_URL`, `VITE_TILE_URL_DARK`, `VITE_TILE_ATTRIBUTION` (all empty by default) and export them as `ENV`.
   4. `npm run build`, which is `tsc -b && vite build`, so a type error fails the image. The output goes to `dist/` with source maps, target ES2022.
2. **Runtime stage** `nginxinc/nginx-unprivileged:1.27-alpine`:
   - `ENV DIVERT_API_UPSTREAM=api:8000`, `NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1`, `NGINX_ENVSUBST_FILTER=^(DIVERT_API_UPSTREAM|NGINX_LOCAL_RESOLVERS)$`
   - The template goes to `/etc/nginx/templates/`, `dist` to `/usr/share/nginx/html`
   - `EXPOSE 8080`, `USER 101`
   - `HEALTHCHECK` with `wget /nginx-health` every 15 s, timeout 3 s. Compose replaces it with `/healthz` through the proxy.

`VITE_API_KEY` is not a Dockerfile `ARG`, so it cannot be baked into the image without editing the Dockerfile.

### 5.2 nginx (`frontend/nginx/default.conf.template`)

```mermaid
flowchart LR
    B["Browser"] -->|":8080 same origin"| N["nginx (frontend container)"]
    N -->|"/, /assets/*, /index.html"| S["static dist/"]
    N -->|"/api/* incl. WebSocket upgrade"| A["DIVERT_API_UPSTREAM (api:8000)"]
    N -->|"/healthz, /readyz"| A
    N -->|"/nginx-health"| H["200 ok (nginx only)"]
```

- **envsubst at start.** The image entrypoint renders the template. Only `DIVERT_API_UPSTREAM` and `NGINX_LOCAL_RESOLVERS` are substituted, so nginx's own `$variables` survive.
- **Runtime DNS for the upstream.** `set $api_upstream http://${DIVERT_API_UPSTREAM}` plus `proxy_pass $api_upstream` and `resolver ${NGINX_LOCAL_RESOLVERS} valid=10s`. The API name is resolved per request and re-resolved every 10 s, which has three effects:
  - nginx starts even when the API does not exist yet
  - it follows a recreated API container or pod
  - it spreads requests over every address of a scaled service

  The resolver applies no search domains, so k8s sets a fully qualified name.
- **WebSocket.** `Upgrade` and `Connection` come from a `map`. `proxy_http_version 1.1`, read/send timeouts 3600 s, `proxy_buffering off`.
- **Request id.** `X-Request-ID $request_id` is sent upstream, so API logs and error bodies carry nginx's id.
- **Caching.** `/assets/*` (content-hashed) is `immutable` for one year. `/index.html` is `no-cache`, so a deploy shows up immediately. Unknown paths fall back to `index.html`.
- **Security headers** on every location: `Content-Security-Policy` (`script-src 'self'`, `style-src 'self' 'unsafe-inline'`, `img-src 'self' data: https:` for tiles, `connect-src 'self' ws: wss:`, `frame-ancestors 'none'`, ...), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`. nginx drops inherited `add_header` lines in locations that declare their own, so those locations repeat them.
- Because the browser only talks to its own origin, no API host is baked into the bundle and CORS is not needed.

In development, `npm run dev` (port 5173) proxies `/api` (WebSocket included), `/readyz` and `/healthz` to `DIVERT_DEV_API` (default `http://localhost:8000`). The dev server sends no CSP.

---

## 6. Conventions

- **Coordinates are `[lat, lng]` tuples** (`LatLng` in `api/types.ts`) everywhere, matching the API. Leaflet takes the same order.
- **`api/types.ts` mirrors [api.md](../api.md) field for field.** Shared enums are `as const` arrays plus derived union types (`INCIDENT_TYPES`, `SERVICE_TYPES`, `INCIDENT_STATUSES`). Lookup maps are typed `Record<Union, ...>`, so a new enum value makes `tsc` list every map that needs an entry.
- **Imperative Leaflet in effects.** Create layers in a `useEffect`, keep handles in a closure `Map`, and clean up in the effect's return. Read high-frequency state with `getState()` and `subscribe`, not with hooks.
- **Accessibility.** Tabs use `role="tablist"` / `tab` / `tabpanel`, there is a skip link, panels are `section`s labelled by their heading, and `Segmented` is a radio group with arrow keys. `eslint-plugin-jsx-a11y` is enforced.
- **Formatting and labels** come from `lib/format.ts`. Colours come from `lib/theme.ts`. Times are shown in the city's time zone (`CityDetail.timezone`).

---

## 7. Recipes

### 7.1 Add a panel

1. Create `src/features/<name>/<Name>Panel.tsx`. Wrap it in `Panel` from `components/ui/Panel.tsx` (`title`, optional `actions`, and an `id` so the heading labels the section). Use `EmptyState`, `Spinner` and `Stat` for common states.
2. Read data:
   - live data with narrow selectors: `useLiveStore((s) => s.vehicles)`
   - REST data through a hook in `api/queries.ts`
   - UI state through `useUiStore`

   Use `useMemo` for derived lists. Do not subscribe to the whole store.
3. Place it in `app/LiveView.tsx`. The grid is `lg:grid-cols-[19rem,minmax(0,1fr),21rem]`: the left `aside` has dispatch and stats, the centre has the map, the right `aside` has incidents and fleet. For the other tabs, add it inside `HistoryView` or `AnalyticsView`.
4. Test with `renderWithProviders(<NamePanel city={DELHI} />)`. Seed live state with `useLiveStore.getState().reset('delhi')` and then `apply({type: 'snapshot', ...})`. Stub REST with `mockFetch([...])`. `FleetPanel.test.tsx` is the model.

### 7.2 Add a tab

1. Add the id to `TABS` in `src/store/ui.ts`. The `Tab` type and `parseHash` follow automatically.
2. Add its label to `TAB_LABEL` in `src/app/App.tsx`. It is a `Record<Tab, string>`, so `tsc` insists.
3. Add a render branch in `App`'s `<main>` (currently a nested ternary over `tab`). Key the view by `data.id` if it holds per-city local state.
4. The live stream already runs on every tab (`useCityStream` in `App`). Deep links work as `#/<tab>/<city>`.
5. Add a test that renders the view with `renderWithProviders`.

### 7.3 Add an API call (with its types)

1. Types: add the request and response interfaces to `src/api/types.ts`, mirroring [api.md](../api.md). Use `LatLng` for coordinates and `IsoTime` for timestamps.
2. Client: add a method to the `api` object in `src/api/client.ts`:

   ```ts
   thing: (city: string, q: ThingQuery = {}, signal?: AbortSignal) =>
     request<ThingResponse>(v1(`/cities/${enc(city)}/thing`) + buildQuery({ ...q }), { signal }),
   ```

   For writes, use `{ method: 'POST' | 'DELETE', body }`. `request` then adds `Content-Type` and the API key. Only `GET`, `POST` and `DELETE` exist, which matches the backend CORS methods.
3. Hook: add a key to `queryKeys` and a `useQuery` / `useMutation` hook in `src/api/queries.ts`. Pass `signal` for cancellation. Invalidate related keys in `onSettled` for mutations.
4. Errors: new backend error codes get a friendly message in `describeError` (`client.ts`).
5. If the live stream should refresh it, add its key to the debounced invalidation in `src/api/useCityStream.ts`.
6. Tests: a case in `src/api/client.test.ts` using `mockFetch` and `urlOf`.

### 7.4 Consume a new WebSocket message

The backend side is in [Backend codebase](09-codebase-backend.md) section 7.5.

1. `src/api/types.ts`: add `interface XMessage extends Envelope<'x'> { city: CityId; ... }` and add it to the `StreamMessage` union.
2. `src/api/stream.ts`: add `'x'` to `KNOWN_TYPES`. **Without this, `parseMessage` silently drops the message.**
3. `src/store/live.ts`: add any new state to `LiveState` and `initial`, and a `case 'x':` in `apply`. Keep it a pure state update, and return early for messages that change nothing.
4. `src/api/useCityStream.ts`: add side effects if needed, for example `scheduleInvalidate()` for REST views.
5. Tests: `stream.test.ts` (the type is accepted) and `live.test.ts` (`apply` produces the expected state).
6. Bump `STREAM_SCHEMA_VERSION` only together with the backend `store/events.SCHEMA_VERSION`, for breaking changes. Clients drop messages with a newer `v`.

---

## Related

- [Backend codebase](09-codebase-backend.md): the server side of every contract used here
- [Configuration](11-configuration.md): `VITE_*`, `DIVERT_WEB_*` and nginx variables
- [Data flows](06-data-flows.md), [Runtime workflows](07-runtime-workflows.md): the live path and WebSocket behaviour end to end
- [Engineering workflows](08-engineering-workflows.md): running the dev server, tests and CI
- [Domain rules](02-domain-rules.md), [Glossary](12-glossary.md)
- Reference: [api.md](../api.md), [frontend/README.md](../../frontend/README.md), [deployment.md](../deployment.md)
