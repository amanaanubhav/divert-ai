# DIVERT ops console

React 19 + TypeScript (strict) + Vite 8 + Tailwind 3 + react-leaflet 5 + zustand 5 +
TanStack Query 5. Tests: vitest + Testing Library. Lint: ESLint 10 flat config
(typescript-eslint strict type-checked, jsx-a11y, react-hooks) + Prettier.

```bash
npm ci
npm run dev          # http://localhost:5173, proxies /api (REST + WS) to http://localhost:8000
npm run lint         # eslint, zero warnings allowed
npm run typecheck    # tsc -b --noEmit
npm test             # vitest (jsdom)
npm run build        # tsc -b && vite build -> dist/
npm run format:check # prettier
npm run check:ui     # after build: real-browser check that no control is covered (see below)
```

## UI check

`npm run check:ui` (`scripts/clickability.mjs`) opens Live, History and Analytics for each
city at six viewports (360 to 1920 px wide) in the locally installed Microsoft Edge (via
`playwright-core`, a dev-only dependency; no browser download, nothing shipped) and asserts
that `document.elementFromPoint` at the centre of every visible control hits that control:
nothing covers it. It repeats this with a vehicle selected (its "Why this route" open), a
signal popup open, a dispatch toast, the API-key popover, the History drawer, and a route
explainer open inside the drawer. That is 11 states per city and viewport, 198 in all.
The mocks (`scripts/ui-check-mocks.mjs`) carry every trip-leg state:

- a TRANSPORT leg with a patient and its destination hospital
- a RESPONSE leg with a speed advisory
- a unit on scene
- a unit at the hospital planning its RETURN leg
- a RETURN leg
- the five junction states
- a free left By default it serves `dist/` with `vite preview` and mocks the API and
  WebSocket; `-- --url http://localhost:8080` checks the running stack instead (POST/DELETE
  stay mocked). Screenshots and `results.json` land in `.ui-check/` (gitignored).

## Look

Light by default (dark follows `prefers-color-scheme`), self-hosted Noto Sans, flat white
panels with 1 px borders and 4 px corners, one civic-blue primary (`#1f4e79`), status
colours only for status. Colours: `src/lib/palette.ts` (checked by `palette.test.ts` for
WCAG AA and colour-blind separation). Layers: `src/lib/zIndex.ts`.

`DIVERT_DEV_API=http://host:8000 npm run dev` points the dev proxy at another backend.

## Trip legs (design section 6)

- **Map:** each vehicle's current leg is drawn by kind:
  - RESPONSE: solid, in the service colour.
  - TRANSPORT: solid violet (`#6c35c2`), with a patient badge on the vehicle. The destination hospital is a white square with a violet "H".
  - RETURN: dashed grey.

  Corridor junctions with a free-left slip road get a "free left" tag; they are never claimed.

- **Junctions:** each state has its own shape:
  - transitioning: a hollow green ring with a "green in n s" countdown
  - pre-empted: a solid green dot (the only blink in the UI)
  - queued: an amber diamond
  - recovering: a hollow grey ring

  Clicking a junction opens a popup with the countdowns (transition left, hold, back to plan) and the claimant queue (movement, rank, state, ETA).

- **Fleet card:**
  - a phase chip: Responding, On scene, Transporting, At hospital or Returning
  - the leg and its destination
  - the next movement, as an arrow and text
  - a lane diagram: lanes counted from the left kerb, the target lane filled, "be in lane n within x m"
  - the speed advisory while queued
  - "Planning route…" while a leg plan is pending
  - the dwell time on scene or at the hospital

  The selected vehicle also shows "Why this route"; hovering it draws the alternatives faintly on the map.

- **Incidents:**
  - TRANSPORTING and AT_HOSPITAL are shown with the hospital.
  - Cancel is disabled, with a tooltip, while a patient is on board.
  - A 409 `cannot_cancel_transport` gets its own toast.
- **History drawer:**
  - every leg, with predicted vs actual (simulated seconds, ±10 %)
  - the hospital, and why it was chosen
  - "Why this route" for each leg; hovering it draws the alternatives on the replay map
  - pre-emptions with leg, movement, lane and outcome
  - the timeline, grouped by leg
  - a replay across all legs, with a leg bar and jump buttons
- **Analytics:**
  - transport, call-to-hospital and handover-to-back-in-service times
  - hospital distribution
  - ETA accuracy per leg kind
  - pre-emption outcomes, and green extension vs red truncation
  - free-left passes and re-routes
  - dispatches by trip status

Alternatives are drawn only when the backend sends their geometry (`route_choice.candidates[].route`).
The current backend records each candidate's cost but not its geometry, so the explainer says that instead of drawing anything.

## Contract

`src/api/types.ts` mirrors [docs/api.md](../docs/api.md) field for field (coordinates are
always `[lat, lng]`). Live state comes from the WebSocket `/api/v1/cities/{city}/stream`:

- `snapshot`, on (re)connect, replaces the store.
- `tick` (every 0.5 s) carries the full vehicle list and the complete non-NORMAL signal set.
- `vehicle_added` carries the route and corridor.
- `leg_changed` swaps in the next leg's route; the motion restarts at its start.
- `route_replaced` splices a re-route in.
- `vehicle_removed` and `incident_updated` trigger a debounced refresh of the REST views.

A tick whose leg is newer than the drawn route drops the stale route and back-fills it from `/vehicles`. Unknown message types and fields are ignored; a message with a newer schema `v` is
dropped. Reconnects use exponential backoff with jitter (0.5 s .. 15 s) and stop on close
code 4404 (unknown city).

## Rendering

Reference layers (hundreds of signals, ~1,200 stations per city) are canvas circle markers
(`preferCanvas`), created imperatively once per data change. Vehicles and active signals
are imperative Leaflet markers updated in a `requestAnimationFrame` loop: each vehicle is
interpolated between ticks along its route by cumulative haversine distance
(`src/lib/geo.ts`, the backend's model) and rotated to the route heading, so motion is
smooth without re-rendering React at 60 fps.

## Production

`Dockerfile` builds with node 22 and serves `dist/` from unprivileged nginx on :8080
(`nginx/default.conf.template`: SPA fallback, `/api` + WebSocket upgrade proxied to the
API, CSP and security headers, immutable caching for hashed assets, `no-cache` for
`index.html`). The template is rendered at container start: `DIVERT_API_UPSTREAM`
(default `api:8000` for compose; k8s sets `divert-api.<namespace>.svc.cluster.local:80`)
names the API, and the container's own DNS servers are used to re-resolve it every 10 s,
so nginx starts without the API, follows a recreated API container/pod and spreads load
over scaled replicas.
Build args: `VITE_API_BASE_URL` (default empty = same origin), `VITE_API_KEY` (optional).
