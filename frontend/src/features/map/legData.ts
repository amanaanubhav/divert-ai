import type { HospitalInfo, LatLng, LegKind } from '../../api/types'
import { legDash } from '../../lib/theme'
import type { LiveVehicle } from '../../store/live'

/** Hospitals that are the destination of a transport (or where a unit is handing over). */
export function destinationHospitals(
  vehicles: readonly LiveVehicle[],
): Map<string, { hospital: HospitalInfo; vehicles: string[] }> {
  const out = new Map<string, { hospital: HospitalInfo; vehicles: string[] }>()
  for (const v of vehicles) {
    const h = v.hospital
    if (!h || (v.phase !== 'TRANSPORTING' && v.phase !== 'AT_HOSPITAL')) continue
    const cur = out.get(h.id)
    if (cur) cur.vehicles.push(v.id)
    else out.set(h.id, { hospital: h, vehicles: [v.id] })
  }
  return out
}

/** Free-left junctions still ahead on each vehicle's current leg (never claimed). */
export function freeLeftAhead(
  vehicles: readonly LiveVehicle[],
): Map<string, { pos: LatLng; signalId: string; vehicleId: string }> {
  const out = new Map<string, { pos: LatLng; signalId: string; vehicleId: string }>()
  for (const v of vehicles) {
    for (const c of v.corridor ?? []) {
      if (!c.free_left || c.along_m < v.along_m) continue
      out.set(`${v.id}|${c.signal_id}|${c.pass_no ?? 0}`, {
        pos: c.pos,
        signalId: c.signal_id,
        vehicleId: v.id,
      })
    }
  }
  return out
}

/** Polyline styles of one leg: RESPONSE solid service colour, TRANSPORT solid violet, RETURN dashed grey. */
export function legLineStyles(kind: LegKind | undefined, color: string) {
  const dash = legDash(kind)
  return {
    travelled: { color, weight: 3, opacity: 0.35, dashArray: '4 6' },
    casing: { color: '#ffffff', weight: kind === 'RETURN' ? 6 : 7.5, opacity: 0.75 },
    remaining: {
      color,
      weight: kind === 'RETURN' ? 3.5 : 4.5,
      opacity: 0.95,
      ...(dash ? { dashArray: dash } : {}),
    },
  }
}
