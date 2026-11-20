import L from 'leaflet'
import { useEffect } from 'react'
import { useMap } from 'react-leaflet'
import type { HospitalInfo } from '../../api/types'
import { edLabel, hospitalLabel } from '../../lib/format'
import { LEG_COLOR } from '../../lib/theme'
import { useLiveStore, type LiveVehicle } from '../../store/live'
import { useUiStore, type RoutePreview } from '../../store/ui'
import { escapeHtml, freeLeftIcon, hospitalIcon } from './icons'
import { destinationHospitals, freeLeftAhead } from './legData'

function hospitalTip(h: HospitalInfo, vehicles: readonly string[]): string {
  return (
    `<strong>Destination hospital</strong>: ${escapeHtml(hospitalLabel(h))}<br>` +
    `<span class="tip-meta">${escapeHtml(edLabel(h.emergency))} · ${escapeHtml(vehicles.join(', '))}</span>`
  )
}

/**
 * Leg extras on the live map: the destination hospital of every transport (highlighted) and a
 * "free left" tag at corridor junctions whose slip road bypasses the signal.
 */
export function LegExtrasLayer() {
  const map = useMap()
  useEffect(() => {
    const group = L.layerGroup().addTo(map)
    const hospitals = new Map<string, { marker: L.Marker; key: string }>()
    const freeLefts = new Map<string, L.Marker>()

    const sync = (vehicles: Record<string, LiveVehicle>) => {
      const list = Object.values(vehicles)
      const wanted = destinationHospitals(list)
      for (const [id, h] of hospitals)
        if (!wanted.has(id)) {
          h.marker.remove()
          hospitals.delete(id)
        }
      for (const [id, w] of wanted) {
        const key = w.vehicles.join(',')
        const cur = hospitals.get(id)
        if (cur?.key === key) continue
        const tip = hospitalTip(w.hospital, w.vehicles)
        if (cur) {
          cur.marker.setTooltipContent(tip)
          cur.key = key
          continue
        }
        const marker = L.marker(w.hospital.location as [number, number], {
          icon: hospitalIcon(),
          keyboard: false,
          zIndexOffset: 400,
          alt: `Destination hospital ${hospitalLabel(w.hospital)}`,
        })
          .bindTooltip(tip, { className: 'divert-tip', direction: 'top' })
          .addTo(group)
        hospitals.set(id, { marker, key })
      }

      const ahead = freeLeftAhead(list)
      for (const [k, m] of freeLefts)
        if (!ahead.has(k)) {
          m.remove()
          freeLefts.delete(k)
        }
      for (const [k, f] of ahead) {
        if (freeLefts.has(k)) continue
        const marker = L.marker(f.pos as [number, number], {
          icon: freeLeftIcon(),
          keyboard: false,
          interactive: false,
          zIndexOffset: 300,
        }).addTo(group)
        freeLefts.set(k, marker)
      }
    }

    sync(useLiveStore.getState().vehicles)
    const unsubscribe = useLiveStore.subscribe((s, prev) => {
      if (s.vehicles !== prev.vehicles) sync(s.vehicles)
    })
    return () => {
      unsubscribe()
      group.remove()
    }
  }, [map])
  return null
}

/** Faint alternatives of the selected vehicle's route while "Why this route" is hovered. */
export function RoutePreviewLayer() {
  const map = useMap()
  useEffect(() => {
    const group = L.layerGroup().addTo(map)
    const draw = (routes: RoutePreview[] | null) => {
      group.clearLayers()
      for (const r of routes ?? []) drawPreview(group, r)
    }
    draw(useUiStore.getState().routePreview)
    const unsubscribe = useUiStore.subscribe((s, prev) => {
      if (s.routePreview !== prev.routePreview) draw(s.routePreview)
    })
    return () => {
      unsubscribe()
      group.remove()
    }
  }, [map])
  return null
}

/** One alternative: a thin dashed neutral line with its label (A, B, ...) at the middle. */
function drawPreview(group: L.LayerGroup, r: RoutePreview): void {
  const pts = r.route as [number, number][]
  L.polyline(pts, {
    color: LEG_COLOR.RETURN,
    weight: r.emphasis ? 5 : 4,
    opacity: r.emphasis ? 0.85 : 0.5,
    dashArray: '2 7',
    lineCap: 'round',
    interactive: false,
  }).addTo(group)
  const mid = pts[Math.floor(pts.length / 2)]
  if (mid)
    L.marker(mid, { icon: L.divIcon({ className: '', iconSize: [0, 0] }), interactive: false })
      .bindTooltip(`Route ${escapeHtml(r.label)}`, {
        permanent: true,
        direction: 'center',
        className: 'divert-tip',
      })
      .addTo(group)
}
