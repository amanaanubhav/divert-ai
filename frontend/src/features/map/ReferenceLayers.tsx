import L from 'leaflet'
import { useEffect } from 'react'
import { useMap } from 'react-leaflet'
import type { Signal, Station } from '../../api/types'
import { SERVICE_LABEL } from '../../lib/format'
import { SERVICE_COLOR, SIGNAL_COLOR } from '../../lib/theme'
import { escapeHtml } from './icons'

/** Marker radius by zoom: subdued when zoomed out, easy to hit when zoomed in. */
function radiusFor(kind: 'signal' | 'station', zoom: number): number {
  const t = Math.max(0, Math.min(1, (zoom - 10) / 6)) // 0 at z10, 1 at z16
  return kind === 'signal' ? 1.6 + t * 3 : 2.2 + t * 3.8
}

function scaleWithZoom(
  map: L.Map,
  markers: L.CircleMarker[],
  kind: 'signal' | 'station',
): () => void {
  const apply = () => {
    const r = radiusFor(kind, map.getZoom())
    for (const m of markers) m.setRadius(r)
  }
  apply()
  map.on('zoomend', apply)
  return () => map.off('zoomend', apply)
}

/**
 * All junction signals of the city as canvas circle markers (hundreds to thousands stay
 * smooth: one canvas, no DOM node per marker, tooltips created lazily on hover).
 */
export function SignalsLayer({ signals }: { signals: readonly Signal[] }) {
  const map = useMap()
  useEffect(() => {
    const group = L.layerGroup()
    const markers: L.CircleMarker[] = []
    for (const s of signals) {
      const m = L.circleMarker(s.location as [number, number], {
        radius: 2.6,
        color: '#ffffff',
        weight: 0.8,
        fillColor: SIGNAL_COLOR.NORMAL,
        fillOpacity: 0.95,
        bubblingMouseEvents: true,
      })
      markers.push(m)
      m.bindTooltip(
        () =>
          `<strong>${escapeHtml(s.name ?? 'Signalised junction')}</strong><br><span class="font-mono text-[10px] text-slate-400">${escapeHtml(s.id)} · ${s.osm_node_ids.length} OSM node${s.osm_node_ids.length === 1 ? '' : 's'}</span>`,
        { className: 'divert-tip', direction: 'top', offset: [0, -3] },
      ).addTo(group)
    }
    group.addTo(map)
    const unscale = scaleWithZoom(map, markers, 'signal')
    return () => {
      unscale()
      group.remove()
    }
  }, [map, signals])
  return null
}

/** Stations coloured by service; hollow when no unit is free. */
export function StationsLayer({ stations }: { stations: readonly Station[] }) {
  const map = useMap()
  useEffect(() => {
    const group = L.layerGroup()
    const markers: L.CircleMarker[] = []
    for (const st of stations) {
      const color = SERVICE_COLOR[st.service]
      const m = L.circleMarker(st.location as [number, number], {
        radius: 4.5,
        color,
        weight: 1.2,
        opacity: 0.8,
        fillColor: color,
        fillOpacity: st.free_units > 0 ? 0.45 : 0.05,
        bubblingMouseEvents: true,
      })
      markers.push(m)
      m.bindTooltip(
        () =>
          `<strong>${escapeHtml(st.name ?? 'Unnamed station')}</strong><br>${SERVICE_LABEL[st.service]} · <span class="${st.free_units > 0 ? 'text-emerald-300' : 'text-red-300'}">${st.free_units}/${st.total_units} free</span>`,
        { className: 'divert-tip', direction: 'top', offset: [0, -4] },
      ).addTo(group)
    }
    group.addTo(map)
    const unscale = scaleWithZoom(map, markers, 'station')
    return () => {
      unscale()
      group.remove()
    }
  }, [map, stations])
  return null
}

/** Dashed outline of the service area (`[south, west, north, east]`). */
export function CityBoundsLayer({ bbox }: { bbox: readonly [number, number, number, number] }) {
  const map = useMap()
  useEffect(() => {
    const [s, w, n, e] = bbox
    const rect = L.rectangle(
      [
        [s, w],
        [n, e],
      ],
      { color: '#f43f5e', weight: 1.2, dashArray: '6 6', fill: false, interactive: false },
    ).addTo(map)
    return () => {
      rect.remove()
    }
  }, [map, bbox])
  return null
}
