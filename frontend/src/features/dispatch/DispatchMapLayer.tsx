import type { Marker as LeafletMarker } from 'leaflet'
import { useEffect, useMemo } from 'react'
import { Marker, Polyline, Tooltip, useMap, useMapEvents } from 'react-leaflet'
import type { CityDetail } from '../../api/types'
import { toast } from '../../components/ui/toast'
import { formatDistance } from '../../lib/format'
import { useUiStore } from '../../store/ui'
import { MAP_COLOR } from '../../lib/theme'
import { pendingIcon } from '../map/icons'
import { checkLocation, nearestStationFor } from './nearest'

const PENDING_ICON = pendingIcon()

/**
 * Map side of dispatching: a click inside the city sets the pending incident location
 * (outside -> warning, nothing sent); the pending pin is draggable and shows a straight-line
 * preview to the nearest station of the armed service.
 */
export function DispatchMapLayer({ city }: { city: CityDetail }) {
  const map = useMap()
  const pending = useUiStore((s) => s.pending)
  const type = useUiStore((s) => s.dispatchType)
  const setPending = useUiStore((s) => s.setPending)

  useEffect(() => {
    const el = map.getContainer()
    el.classList.add('dispatch-armed')
    return () => el.classList.remove('dispatch-armed')
  }, [map])

  useMapEvents({
    click(e) {
      const p = [e.latlng.lat, e.latlng.lng] as const
      const check = checkLocation(p, city.bbox, city.name)
      if (!check.ok) {
        toast.warning('Outside service area', check.reason)
        return
      }
      setPending(p)
    },
  })

  const near = useMemo(
    () => (pending ? nearestStationFor(pending, type, city.stations) : null),
    [pending, type, city.stations],
  )

  if (!pending) return null
  return (
    <>
      {near ? (
        <Polyline
          positions={[pending as [number, number], near.station.location as [number, number]]}
          pathOptions={{ color: MAP_COLOR.pending, weight: 2, dashArray: '2 6', opacity: 0.9 }}
          interactive={false}
        />
      ) : null}
      <Marker
        position={pending as [number, number]}
        icon={PENDING_ICON}
        draggable
        keyboard={false}
        zIndexOffset={3000}
        eventHandlers={{
          dragend(e) {
            const ll = (e.target as LeafletMarker).getLatLng()
            const p = [ll.lat, ll.lng] as const
            const check = checkLocation(p, city.bbox, city.name)
            if (check.ok) setPending(p)
            else {
              toast.warning('Outside service area', check.reason)
              setPending([pending[0], pending[1]]) // new tuple: snaps the pin back
            }
          },
        }}
      >
        {near ? (
          <Tooltip className="divert-tip" direction="right" offset={[12, -16]} permanent>
            {formatDistance(near.distance_m)} to {near.station.name ?? near.station.id}
          </Tooltip>
        ) : null}
      </Marker>
    </>
  )
}
