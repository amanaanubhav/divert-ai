import type { BBox, IncidentType, LatLng, Station } from '../../api/types'
import { SERVICE_FOR_TYPE } from '../../api/types'
import { insideBBox, nearest } from '../../lib/geo'

export interface NearestStation {
  station: Station
  distance_m: number
  /** `false` when no station of that service has a free unit (nearest of all is shown). */
  hasFreeUnit: boolean
}

/**
 * Nearest station (straight-line haversine) able to answer an incident of `type`,
 * preferring stations with a free unit. The server picks by routed ETA; this is a preview.
 */
export function nearestStationFor(
  p: LatLng,
  type: IncidentType,
  stations: readonly Station[],
): NearestStation | null {
  const service = SERVICE_FOR_TYPE[type]
  const ofService = stations.filter((s) => s.service === service)
  const free = nearest(
    p,
    ofService.filter((s) => s.free_units > 0),
    (s) => s.location,
  )
  if (free) return { station: free.item, distance_m: free.distance_m, hasFreeUnit: true }
  const any = nearest(p, ofService, (s) => s.location)
  return any ? { station: any.item, distance_m: any.distance_m, hasFreeUnit: false } : null
}

export type LocationCheck = { ok: true } | { ok: false; reason: string }

export function checkLocation(p: LatLng, bbox: BBox, cityName: string): LocationCheck {
  return insideBBox(p, bbox)
    ? { ok: true }
    : { ok: false, reason: `That point is outside the ${cityName} service area.` }
}
