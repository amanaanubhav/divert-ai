import L from 'leaflet'
import { useEffect } from 'react'
import { useMap } from 'react-leaflet'
import type { ActiveSignal } from '../../api/types'
import { useLiveStore } from '../../store/live'
import { signalIcon } from '../map/icons'
import {
  describeSignal,
  junctionState,
  popupHtml,
  popupModel,
  signalLabel,
  signalTooltip,
  stateClass,
} from './signalText'

interface Drawn {
  marker: L.Marker
  root: HTMLElement | null
  label: HTMLElement | null
  approach: HTMLElement | null
  key: string
  popupKey: string
}

const STATE_CLASSES = ['is-transitioning', 'is-preempted', 'is-queued', 'is-recovering']
/** Popup width (px): wide enough for the claimant table without wrapping vehicle ids. */
const POPUP_MIN_WIDTH = 330
const POPUP_MAX_WIDTH = 440
/** Space kept free around the popup on small maps (px). */
const POPUP_EDGE_PX = 48
/** Below this the popup does not shrink further (px); its content scrolls. */
const POPUP_MIN_HEIGHT = 160
/** Smallest scroll area when the junction sits near the top of a small map (px). */
const POPUP_TIGHT_HEIGHT = 90
/** Popup chrome above the marker: tip, content margins, the marker itself (px). */
const POPUP_CHROME_PX = 64

/**
 * The map cannot always pan the popup into view (its max bounds stop it near the city edge),
 * so the popup's scroll height is fitted to the room above the marker: the title and the close
 * button always stay inside the map; the rest scrolls.
 */
function fitPopupAbove(map: L.Map, marker: L.Marker): void {
  const popup = marker.getPopup()
  if (!popup) return
  const room = map.latLngToContainerPoint(marker.getLatLng()).y - POPUP_CHROME_PX
  const cap = Math.max(POPUP_MIN_HEIGHT, map.getSize().y - POPUP_EDGE_PX * 2)
  const height = Math.max(POPUP_TIGHT_HEIGHT, Math.min(cap, room))
  if (popup.options.maxHeight === height) return
  popup.options.maxHeight = height
  popup.update()
}

/**
 * Junctions that are not running their plan, one marker each, updated in place every tick
 * (so the pre-empted blink never restarts). Each state has its own shape: transitioning =
 * hollow green ring, pre-empted = solid green dot, queued = amber diamond, recovering = hollow
 * grey ring. The label carries the ETA / countdown, the arrow the approach. A click opens a
 * popup with the countdowns and the claimant queue (movement, rank), refreshed while open.
 */
export function ActiveSignalsLayer({ names }: { names?: ReadonlyMap<string, string | null> }) {
  const map = useMap()
  useEffect(() => {
    const drawn = new Map<string, Drawn>()
    const group = L.layerGroup().addTo(map)

    const sync = (signals: Record<string, ActiveSignal>, simTs: string | null) => {
      for (const [id, d] of drawn) {
        if (!signals[id]) {
          d.marker.remove()
          drawn.delete(id)
        }
      }
      for (const s of Object.values(signals)) {
        let d = drawn.get(s.signal_id)
        if (!d) {
          const marker = L.marker(s.pos as [number, number], {
            icon: signalIcon(),
            keyboard: false,
            zIndexOffset: 500,
            interactive: true,
          }).addTo(group)
          marker.bindTooltip('', { className: 'divert-tip', direction: 'top', offset: [0, -12] })
          // never larger than the map (phones): it scrolls inside instead of being clipped
          const size = map.getSize()
          const room = Math.max(200, size.x - POPUP_EDGE_PX)
          marker.bindPopup('', {
            className: 'divert-popup',
            minWidth: Math.min(POPUP_MIN_WIDTH, room),
            maxWidth: Math.min(POPUP_MAX_WIDTH, room),
            maxHeight: Math.max(POPUP_MIN_HEIGHT, size.y - POPUP_EDGE_PX * 2),
            autoPan: true,
          })
          // the popup carries everything the tooltip says: never show both; the map's own
          // controls (layers, legend) step aside while it is open so they never cover it
          const wrapper = map.getContainer().parentElement
          marker.on('popupopen', () => {
            marker.closeTooltip()
            wrapper?.classList.add('map-popup-open')
            fitPopupAbove(map, marker)
          })
          marker.on('popupclose', () => wrapper?.classList.remove('map-popup-open'))
          marker.on('tooltipopen', () => {
            if (marker.isPopupOpen()) marker.closeTooltip()
          })
          const root = marker.getElement()?.querySelector<HTMLElement>('.sig') ?? null
          d = {
            marker,
            root,
            label: root?.querySelector<HTMLElement>('.sig-label') ?? null,
            approach: root?.querySelector<HTMLElement>('.sig-approach') ?? null,
            key: '',
            popupKey: '',
          }
          drawn.set(s.signal_id, d)
        }
        const state = junctionState(s)
        const label = signalLabel(s, simTs)
        const description = describeSignal(s, simTs)
        const key = `${state}|${label}|${description}|${s.approach_bearing}|${s.pos[0]},${s.pos[1]}`
        if (key !== d.key) {
          d.key = key
          d.marker.setLatLng(s.pos as [number, number])
          d.root?.classList.remove(...STATE_CLASSES)
          d.root?.classList.add(stateClass(state))
          if (d.label) d.label.textContent = label
          if (d.approach) {
            d.approach.style.transform = `rotate(${s.approach_bearing}deg)`
            d.approach.hidden = state === 'RECOVERING'
          }
          d.root?.setAttribute('aria-label', description)
          d.marker.setTooltipContent(signalTooltip(s, simTs))
        }
        const html = popupHtml(popupModel(s, simTs, names?.get(s.signal_id)))
        if (html !== d.popupKey) {
          d.popupKey = html
          d.marker.setPopupContent(html)
        }
      }
    }

    const st = useLiveStore.getState()
    sync(st.signals, st.simTs)
    const unsubscribe = useLiveStore.subscribe((s, prev) => {
      if (s.signals !== prev.signals) sync(s.signals, s.simTs)
    })
    return () => {
      unsubscribe()
      group.remove()
      drawn.clear()
    }
  }, [map, names])
  return null
}
