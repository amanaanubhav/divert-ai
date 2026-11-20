import clsx from 'clsx'
import { MapContainer, ZoomControl } from 'react-leaflet'
import type { CityDetail, Incident } from '../../api/types'
import { SERVICE_LABEL } from '../../lib/format'
import { SERVICE_COLOR, SIGNAL_COLOR } from '../../lib/theme'
import { useUiStore, type LayerToggles } from '../../store/ui'
import { DispatchMapLayer } from '../dispatch/DispatchMapLayer'
import { ActiveSignalsLayer } from '../signals/ActiveSignalsLayer'
import { LiveVehiclesLayer } from './LiveVehiclesLayer'
import { BaseTiles, FitToBBox, FocusController, IncidentsLayer } from './MapHelpers'
import { CityBoundsLayer, SignalsLayer, StationsLayer } from './ReferenceLayers'

export function LiveMap({ city, incidents }: { city: CityDetail; incidents: readonly Incident[] }) {
  const layers = useUiStore((s) => s.layers)
  const dark = useUiStore((s) => s.darkTiles)
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
        <IncidentsLayer incidents={incidents} />
        <ActiveSignalsLayer />
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
    <div className="pointer-events-none absolute left-2 top-2 z-map-overlay flex flex-col gap-2">
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
      <div
        className={clsx(
          'pointer-events-auto hidden rounded border border-line bg-surface px-2 py-1.5 text-xs text-fg sm:block',
        )}
        aria-label="Legend"
        role="group"
      >
        <ul className="space-y-1">
          {(Object.keys(SERVICE_COLOR) as (keyof typeof SERVICE_COLOR)[]).map((s) => (
            <li key={s} className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className="h-2.5 w-2.5 rounded-full dark:ring-1 dark:ring-white/40"
                style={{ background: SERVICE_COLOR[s] }}
              />
              {SERVICE_LABEL[s]}
            </li>
          ))}
          <li className="flex items-center gap-2">
            <span
              aria-hidden="true"
              className="h-2.5 w-2.5 rounded-full"
              style={{ background: SIGNAL_COLOR.PREEMPTED }}
            />
            Pre-empted signal
          </li>
          <li className="flex items-center gap-2">
            <span
              aria-hidden="true"
              className="ml-px h-2 w-2 rotate-45 rounded-[1px]"
              style={{ background: SIGNAL_COLOR.CONFLICT }}
            />
            Conflict
          </li>
          <li className="flex items-center gap-2">
            <span
              aria-hidden="true"
              className="h-2 w-2 rounded-full"
              style={{ background: SIGNAL_COLOR.NORMAL }}
            />
            Signal (normal)
          </li>
        </ul>
      </div>
    </div>
  )
}
