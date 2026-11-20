import { useMemo, useState, type ReactNode } from 'react'
import { MapContainer, ZoomControl } from 'react-leaflet'
import type { CityDetail, Incident } from '../../api/types'
import { SERVICE_LABEL } from '../../lib/format'
import { LEG_COLOR, SERVICE_COLOR, SIGNAL_COLOR } from '../../lib/theme'
import { useUiStore, type LayerToggles } from '../../store/ui'
import { DispatchMapLayer } from '../dispatch/DispatchMapLayer'
import { ActiveSignalsLayer } from '../signals/ActiveSignalsLayer'
import { LegExtrasLayer, RoutePreviewLayer } from './LegLayers'
import { LiveVehiclesLayer } from './LiveVehiclesLayer'
import { BaseTiles, FitToBBox, FocusController, IncidentsLayer } from './MapHelpers'
import { CityBoundsLayer, SignalsLayer, StationsLayer } from './ReferenceLayers'

/** The legend starts open where the map has room for it (xl); it can always be toggled. */
const LEGEND_OPEN_QUERY = '(min-width: 1280px)'

export function LiveMap({ city, incidents }: { city: CityDetail; incidents: readonly Incident[] }) {
  const layers = useUiStore((s) => s.layers)
  const dark = useUiStore((s) => s.darkTiles)
  const signalNames = useMemo(
    () => new Map(city.signals.map((s) => [s.id, s.name] as const)),
    [city.signals],
  )
  return (
    <div className="relative h-full min-h-[18rem] overflow-hidden rounded border border-line">
      <MapContainer
        center={city.center as [number, number]}
        zoom={city.zoom}
        preferCanvas
        zoomSnap={0.25}
        zoomDelta={0.5}
        zoomControl={false}
        className="h-full w-full"
        worldCopyJump={false}
        maxBoundsViscosity={0.8}
        attributionControl
      >
        <BaseTiles dark={dark} />
        <ZoomControl position="bottomright" />
        <FitToBBox bbox={city.bbox} />
        <FocusController />
        <CityBoundsLayer bbox={city.bbox} />
        {/* canvas draw order = add order: stations under signals */}
        {layers.stations ? <StationsLayer stations={city.stations} /> : null}
        {layers.signals ? <SignalsLayer signals={city.signals} /> : null}
        <RoutePreviewLayer />
        <IncidentsLayer incidents={incidents} />
        <LegExtrasLayer />
        <ActiveSignalsLayer names={signalNames} />
        <LiveVehiclesLayer showRoutes={layers.routes} />
        <DispatchMapLayer city={city} />
      </MapContainer>
      <MapControls signalCount={city.signals.length} stationCount={city.stations.length} />
    </div>
  )
}

function MapControls({ signalCount, stationCount }: { signalCount: number; stationCount: number }) {
  const layers = useUiStore((s) => s.layers)
  const toggle = useUiStore((s) => s.toggleLayer)
  const dark = useUiStore((s) => s.darkTiles)
  const setDark = useUiStore((s) => s.setDarkTiles)
  const items: { key: keyof LayerToggles; label: string }[] = [
    { key: 'signals', label: `Signals (${signalCount})` },
    { key: 'stations', label: `Stations (${stationCount})` },
    { key: 'routes', label: 'Routes' },
  ]
  return (
    <div className="map-controls pointer-events-none absolute left-2 top-2 z-map-overlay flex max-h-[calc(100%-1rem)] flex-col gap-2">
      <fieldset className="pointer-events-auto rounded border border-line bg-surface px-2 py-1.5 text-xs text-fg">
        <legend className="sr-only">Map layers</legend>
        <div className="flex flex-col gap-1">
          {items.map((it) => (
            <label key={it.key} className="flex cursor-pointer items-center gap-2 py-0.5">
              <input
                type="checkbox"
                className="h-3.5 w-3.5 accent-primary"
                checked={layers[it.key]}
                onChange={() => toggle(it.key)}
              />
              {it.label}
            </label>
          ))}
          <label className="flex cursor-pointer items-center gap-2 py-0.5">
            <input
              type="checkbox"
              className="h-3.5 w-3.5 accent-primary"
              checked={dark}
              onChange={() => setDark(!dark)}
            />
            Dark basemap
          </label>
        </div>
      </fieldset>
      <Legend />
    </div>
  )
}

function Legend() {
  const [open] = useState(
    () => typeof matchMedia === 'function' && matchMedia(LEGEND_OPEN_QUERY).matches,
  )
  return (
    <details
      open={open}
      className="pointer-events-auto hidden min-h-0 overflow-y-auto rounded border border-line bg-surface text-xs text-fg sm:block"
    >
      <summary className="cursor-pointer select-none px-2 py-1 font-medium">Legend</summary>
      <ul className="space-y-1 px-2 pb-1.5" aria-label="Legend">
        {(Object.keys(SERVICE_COLOR) as (keyof typeof SERVICE_COLOR)[]).map((s) => (
          <Item key={s} label={SERVICE_LABEL[s]}>
            <span
              className="h-2.5 w-2.5 rounded-full dark:ring-1 dark:ring-white/40"
              style={{ background: SERVICE_COLOR[s] }}
            />
          </Item>
        ))}
        <Item label="Response leg (service colour)">
          <Line color={SERVICE_COLOR.AMBULANCE} />
        </Item>
        <Item label="Transport leg, patient on board">
          <Line color={LEG_COLOR.TRANSPORT} />
        </Item>
        <Item label="Return leg (obeys signals)">
          <Line color={LEG_COLOR.RETURN} dashed />
        </Item>
        <Item label="Destination hospital">
          <span className="hosp hosp-legend">H</span>
        </Item>
        <Item label="Transitioning to green">
          <span className="sig-key is-transitioning" />
        </Item>
        <Item label="Pre-empted (green held)">
          <span className="sig-key is-preempted" />
        </Item>
        <Item label="Queued (conflict)">
          <span className="sig-key is-queued" />
        </Item>
        <Item label="Recovering">
          <span className="sig-key is-recovering" />
        </Item>
        <Item label="Free left (not claimed)">
          <span className="free-left-dot" />
        </Item>
        <Item label="Signal (normal)">
          <span className="h-2 w-2 rounded-full" style={{ background: SIGNAL_COLOR.NORMAL }} />
        </Item>
      </ul>
    </details>
  )
}

function Item({ label, children }: { label: string; children: ReactNode }) {
  return (
    <li className="flex items-center gap-2">
      <span aria-hidden="true" className="flex w-5 shrink-0 items-center justify-center">
        {children}
      </span>
      {label}
    </li>
  )
}

function Line({ color, dashed = false }: { color: string; dashed?: boolean }) {
  return (
    <svg width="20" height="6" viewBox="0 0 20 6">
      <line
        x1="1"
        y1="3"
        x2="19"
        y2="3"
        stroke={color}
        strokeWidth="3"
        strokeLinecap="round"
        strokeDasharray={dashed ? '4 3' : undefined}
      />
    </svg>
  )
}
