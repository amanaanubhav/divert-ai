import L from 'leaflet'
import { useEffect } from 'react'
import { useMap } from 'react-leaflet'
import type { ActiveSignal } from '../../api/types'
import { useLiveStore } from '../../store/live'
import { signalIcon } from '../map/icons'
import { describeSignal, signalLabel } from './signalText'

interface Drawn {
  marker: L.Marker
  root: HTMLElement | null
  label: HTMLElement | null
  approach: HTMLElement | null
  key: string
}

/**
 * Pre-empted (green dot, blinking) and conflicted (amber diamond) junctions with the ETA +
 * lane label and an arrow on the approach side. Markers are created once per signal and
 * updated in place each tick so the blink never restarts.
 */
export function ActiveSignalsLayer() {
  const map = useMap()
  useEffect(() => {
    const drawn = new Map<string, Drawn>()
    const group = L.layerGroup().addTo(map)

    const sync = (signals: Record<string, ActiveSignal>) => {
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
          const root = marker.getElement()?.querySelector<HTMLElement>('.sig') ?? null
          d = {
            marker,
            root,
            label: root?.querySelector<HTMLElement>('.sig-label') ?? null,
            approach: root?.querySelector<HTMLElement>('.sig-approach') ?? null,
            key: '',
          }
          drawn.set(s.signal_id, d)
        }
        const label = signalLabel(s)
        const key = `${s.state}|${label}|${s.approach_bearing}|${s.pos[0]},${s.pos[1]}`
        if (key === d.key) continue
        d.key = key
        d.marker.setLatLng(s.pos as [number, number])
        d.root?.classList.toggle('is-conflict', s.state === 'CONFLICT')
        if (d.label) d.label.textContent = label
        if (d.approach) d.approach.style.transform = `rotate(${s.approach_bearing}deg)`
        d.root?.setAttribute('aria-label', describeSignal(s))
        d.marker.setTooltipContent(describeSignal(s))
      }
    }

    sync(useLiveStore.getState().signals)
    const unsubscribe = useLiveStore.subscribe((s, prev) => {
      if (s.signals !== prev.signals) sync(s.signals)
    })
    return () => {
      unsubscribe()
      group.remove()
      drawn.clear()
    }
  }, [map])
  return null
}
