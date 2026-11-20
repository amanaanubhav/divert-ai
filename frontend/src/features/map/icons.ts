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
      <div class="veh-label" hidden>${escapeHtml(id)}</div>
    </div>`,
  })
}

export function signalIcon(): L.DivIcon {
  return L.divIcon({
    className: '',
    iconSize: [24, 24],
    iconAnchor: [12, 12],
    html: `<div class="sig"><span class="sig-ring"></span><span class="sig-approach"><i></i></span><span class="sig-dot"></span><span class="sig-label"></span></div>`,
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
