import L from 'leaflet'
import { useEffect } from 'react'
import { useMap } from 'react-leaflet'
import type { LatLng, LegKind } from '../../api/types'
import { splitAt } from '../../lib/geo'
import { legColor } from '../../lib/theme'
import { useLiveStore, vehiclePose, type LiveState, type LiveVehicle } from '../../store/live'
import { useUiStore } from '../../store/ui'
import { vehicleIcon } from './icons'
import { legLineStyles } from './legData'

interface Drawn {
  marker: L.Marker
  root: HTMLElement | null
  body: HTMLElement | null
  label: HTMLElement | null
  route: readonly LatLng[] | null
  legKind: LegKind | undefined
  casing: L.Polyline | null
  remaining: L.Polyline | null
  travelled: L.Polyline | null
  lastSplitAlong: number
  lastSplitAt: number
  selected: boolean
  onScene: boolean
  patient: boolean
}

const SPLIT_EVERY_MS = 120

/**
 * Live vehicles and their current leg, drawn imperatively: React re-renders on structural
 * changes only, while a requestAnimationFrame loop interpolates every vehicle between ticks
 * along its route by haversine distance (smooth at 60 fps with a 0.5 s tick), rotates it to
 * the route heading, and splits the route into travelled (faded) and remaining (solid). A new
 * leg (or a re-route) brings a new route array and is redrawn in that leg's style; a vehicle
 * with a patient on board carries a patient badge.
 */
export function LiveVehiclesLayer({ showRoutes }: { showRoutes: boolean }) {
  const map = useMap()

  useEffect(() => {
    const drawn = new Map<string, Drawn>()
    const routeGroup = L.layerGroup().addTo(map)
    const markerGroup = L.layerGroup().addTo(map)

    const clearRoute = (d: Drawn) => {
      d.casing?.remove()
      d.remaining?.remove()
      d.travelled?.remove()
      d.casing = d.remaining = d.travelled = null
      d.route = null
    }

    const attachRoute = (d: Drawn, v: LiveVehicle) => {
      const kind = v.leg?.kind
      if (!showRoutes || !v.route || v.route.length < 2) {
        if (d.route && (!v.route || v.route.length < 2)) clearRoute(d)
        return
      }
      if (d.route === v.route && d.legKind === kind) return
      clearRoute(d)
      const styles = legLineStyles(kind, legColor(kind, v.service))
      const pts = v.route as [number, number][]
      d.travelled = L.polyline(pts, { ...styles.travelled, interactive: false }).addTo(routeGroup)
      d.casing = L.polyline(pts, { ...styles.casing, interactive: false }).addTo(routeGroup)
      d.remaining = L.polyline(pts, { ...styles.remaining, interactive: false }).addTo(routeGroup)
      d.route = v.route
      d.legKind = kind
      d.lastSplitAlong = -1
      d.lastSplitAt = 0
    }

    const sync = (state: Pick<LiveState, 'vehicles'>) => {
      for (const [id, d] of drawn) {
        if (state.vehicles[id]) continue
        d.marker.remove()
        clearRoute(d)
        drawn.delete(id)
      }
      for (const v of Object.values(state.vehicles)) {
        let d = drawn.get(v.id)
        if (!d) {
          const marker = L.marker(v.pos as [number, number], {
            icon: vehicleIcon(v.service, v.id),
            keyboard: true,
            title: v.id,
            alt: v.id,
            zIndexOffset: 1000,
            riseOnHover: true,
          }).addTo(markerGroup)
          marker.on('click', () => useUiStore.getState().selectVehicle(v.id))
          const root = marker.getElement()?.querySelector<HTMLElement>('.veh') ?? null
          d = {
            marker,
            root,
            body: root?.querySelector<HTMLElement>('.veh-body') ?? null,
            label: root?.querySelector<HTMLElement>('.veh-label') ?? null,
            route: null,
            legKind: undefined,
            casing: null,
            remaining: null,
            travelled: null,
            lastSplitAlong: -1,
            lastSplitAt: 0,
            selected: false,
            onScene: false,
            patient: false,
          }
          drawn.set(v.id, d)
        }
        attachRoute(d, v)
      }
    }

    sync(useLiveStore.getState())
    const unsubscribe = useLiveStore.subscribe((s, prev) => {
      if (s.vehicles !== prev.vehicles) sync(s)
    })

    let raf = 0
    const frame = () => {
      const now = performance.now()
      const { vehicles } = useLiveStore.getState()
      const selectedId = useUiStore.getState().selectedVehicle
      for (const [id, d] of drawn) {
        const v = vehicles[id]
        if (!v) continue
        const pose = vehiclePose(v, now)
        d.marker.setLatLng(pose.pos as [number, number])
        if (d.body) d.body.style.transform = `rotate(${pose.heading.toFixed(1)}deg)`
        const selected = selectedId === id
        if (selected !== d.selected) {
          d.selected = selected
          d.root?.classList.toggle('is-selected', selected)
          if (d.label) d.label.hidden = !selected
          d.marker.setZIndexOffset(selected ? 2000 : 1000)
        }
        const onScene = v.status === 'ON_SCENE'
        if (onScene !== d.onScene) {
          d.onScene = onScene
          d.root?.classList.toggle('on-scene', onScene)
        }
        const patient = v.phase === 'TRANSPORTING' || v.leg?.kind === 'TRANSPORT'
        if (patient !== d.patient) {
          d.patient = patient
          d.root?.classList.toggle('has-patient', patient)
          d.marker.getElement()?.setAttribute('title', patient ? `${id}, patient on board` : id)
        }
        if (
          d.remaining &&
          d.travelled &&
          d.casing &&
          v.route &&
          v.cum &&
          d.route === v.route &&
          now - d.lastSplitAt >= SPLIT_EVERY_MS &&
          Math.abs(pose.along - d.lastSplitAlong) > 1
        ) {
          const [travelled, remaining] = splitAt(v.route, v.cum, pose.along)
          d.travelled.setLatLngs(travelled as [number, number][])
          d.remaining.setLatLngs(remaining as [number, number][])
          d.casing.setLatLngs(remaining as [number, number][])
          d.lastSplitAlong = pose.along
          d.lastSplitAt = now
        }
      }
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)

    return () => {
      cancelAnimationFrame(raf)
      unsubscribe()
      routeGroup.remove()
      markerGroup.remove()
      drawn.clear()
    }
  }, [map, showRoutes])

  return null
}
