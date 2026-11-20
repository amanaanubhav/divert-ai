import { useEffect } from 'react'
import { Marker, TileLayer, Tooltip, useMap } from 'react-leaflet'
import type { BBox, Incident } from '../../api/types'
import { config } from '../../config'
import { STATUS_LABEL, TYPE_LABEL } from '../../lib/format'
import { useUiStore } from '../../store/ui'
import { incidentIcon } from './icons'

/**
 * Raster basemap with OpenStreetMap attribution. Light mode shows the standard tiles
 * unfiltered; dark mode uses VITE_TILE_URL_DARK or, without one, the light tiles through a
 * CSS filter (`.tiles-dark` in index.css).
 */
export function BaseTiles({ dark }: { dark: boolean }) {
  const darkUrl = config.tiles.dark
  const url = dark && darkUrl ? darkUrl : config.tiles.light
  const className = dark && !darkUrl ? 'tiles-dark' : ''
  return (
    <TileLayer
      key={`${url}|${className}`}
      url={url}
      className={className}
      attribution={config.tiles.attribution}
      maxZoom={config.tiles.maxZoom}
      maxNativeZoom={config.tiles.maxZoom}
    />
  )
}

/**
 * Fit the map to the city on (re)selection and keep panning near it. Leaflet caches the
 * container size, so a ResizeObserver re-measures it (grid/panel layout, window resize)
 * and refits until the operator moves the map.
 */
export function FitToBBox({ bbox, padding = 24 }: { bbox: BBox; padding?: number }) {
  const map = useMap()
  useEffect(() => {
    const [s, w, n, e] = bbox
    const bounds: [[number, number], [number, number]] = [
      [s, w],
      [n, e],
    ]
    let userMoved = false
    const fit = () => {
      map.invalidateSize({ pan: false })
      map.setMinZoom(0)
      map.fitBounds(bounds, { padding: [padding, padding], animate: false })
      map.setMinZoom(Math.max(8, Math.floor(map.getBoundsZoom(bounds)) - 2))
    }
    const onUser = () => {
      userMoved = true
    }
    fit()
    const padLat = (n - s) * 0.35
    const padLng = (e - w) * 0.35
    map.setMaxBounds([
      [s - padLat, w - padLng],
      [n + padLat, e + padLng],
    ])
    map.on('dragstart', onUser)
    const el = map.getContainer()
    el.addEventListener('wheel', onUser, { passive: true })
    let raf = 0
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        if (userMoved) map.invalidateSize({ pan: false })
        else fit()
      })
    })
    ro.observe(el)
    return () => {
      ro.disconnect()
      cancelAnimationFrame(raf)
      map.off('dragstart', onUser)
      el.removeEventListener('wheel', onUser)
    }
  }, [map, bbox, padding])
  return null
}

/** Pans to `useUiStore.focus` requests (from panels outside the map). */
export function FocusController() {
  const map = useMap()
  const focus = useUiStore((s) => s.focus)
  useEffect(() => {
    if (!focus) return
    map.flyTo(focus.pos as [number, number], focus.zoom ?? Math.max(map.getZoom(), 15), {
      duration: 0.6,
    })
  }, [map, focus])
  return null
}

/** Active incidents as type-coloured pins. */
export function IncidentsLayer({ incidents }: { incidents: readonly Incident[] }) {
  return (
    <>
      {incidents.map((inc) => (
        <Marker
          key={inc.id}
          position={inc.location as [number, number]}
          icon={incidentIcon(inc.type, inc.status === 'ON_SCENE')}
          keyboard={false}
          zIndexOffset={200}
        >
          <Tooltip className="divert-tip" direction="top">
            <strong>{TYPE_LABEL[inc.type]}</strong> · {STATUS_LABEL[inc.status]}
            {inc.description ? (
              <div className="max-w-[16rem] truncate">{inc.description}</div>
            ) : null}
          </Tooltip>
        </Marker>
      ))}
    </>
  )
}
