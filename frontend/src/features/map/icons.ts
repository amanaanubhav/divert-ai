import L from 'leaflet'
import type { IncidentType, ServiceType } from '../../api/types'
import { SERVICE_FOR_TYPE } from '../../api/types'
import { SERVICE_COLOR, SERVICE_GLYPH, TYPE_COLOR } from '../../lib/theme'

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`)
}

export function vehicleIcon(service: ServiceType, id: string): L.DivIcon {
  return L.divIcon({
    className: '',
    iconSize: [30, 30],
    iconAnchor: [15, 15],
    html: `<div class="veh" style="--c:${SERVICE_COLOR[service]}">
      <div class="veh-body"><svg width="30" height="30" viewBox="0 0 30 30" aria-hidden="true"><path d="M15 1 L20 9 L10 9 Z" fill="${SERVICE_COLOR[service]}" stroke="#fff" stroke-width="1.2"/></svg></div>
      <div class="veh-chip" aria-hidden="true">${SERVICE_GLYPH[service]}</div>
      <div class="veh-patient" aria-hidden="true">${PATIENT_SVG}</div>
      <div class="veh-label" hidden>${escapeHtml(id)}</div>
    </div>`,
  })
}

/** A plain person figure: the "patient on board" badge (TRANSPORT leg). */
const PATIENT_SVG =
  '<svg width="10" height="10" viewBox="0 0 10 10"><circle cx="5" cy="2.6" r="2" fill="#fff"/><path d="M1.3 9.6 C1.3 6.4 8.7 6.4 8.7 9.6 Z" fill="#fff"/></svg>'

/** The destination hospital of a transport: a white square with a violet border and "H". */
export function hospitalIcon(): L.DivIcon {
  return L.divIcon({
    className: '',
    iconSize: [24, 24],
    iconAnchor: [12, 12],
    tooltipAnchor: [0, -12],
    html: '<div class="hosp" aria-hidden="true">H</div>',
  })
}

/** A small "free left" tag next to a corridor junction whose slip road bypasses the signal. */
export function freeLeftIcon(): L.DivIcon {
  return L.divIcon({
    className: '',
    iconSize: [0, 0],
    iconAnchor: [0, 0],
    html: '<div class="free-left"><span class="free-left-dot"></span><span class="free-left-text">free left</span></div>',
  })
}

export function signalIcon(): L.DivIcon {
  return L.divIcon({
    className: '',
    iconSize: [24, 24],
    iconAnchor: [12, 12],
    html: `<div class="sig"><span class="sig-approach"><i></i></span><span class="sig-dot"></span><span class="sig-label"></span></div>`,
  })
}

const incidentIcons = new Map<string, L.DivIcon>()

export function incidentIcon(type: IncidentType, faded = false): L.DivIcon {
  const key = `${type}|${faded ? 1 : 0}`
  const cached = incidentIcons.get(key)
  if (cached) return cached
  const icon = L.divIcon({
    className: '',
    iconSize: [26, 34],
    iconAnchor: [13, 32],
    tooltipAnchor: [0, -26],
    html: `<div class="pin" style="--c:${TYPE_COLOR[type]};${faded ? 'opacity:.55' : ''}"><div class="pin-head"></div><div class="pin-glyph">${SERVICE_GLYPH[SERVICE_FOR_TYPE[type]]}</div></div>`,
  })
  incidentIcons.set(key, icon)
  return icon
}

export function pendingIcon(): L.DivIcon {
  return L.divIcon({
    className: '',
    iconSize: [26, 34],
    iconAnchor: [13, 32],
    html: `<div class="pin is-pending"><div class="pin-head"></div><div class="pin-glyph">+</div></div>`,
  })
}
