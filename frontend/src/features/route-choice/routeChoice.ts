import type { RouteCandidate, RouteChoice } from '../../api/types'
import type { RoutePreview } from '../../store/ui'

/** A drawable geometry: `candidates[].route` with at least two points (docs/api.md). */
export function hasRoute(c: RouteCandidate): boolean {
  return Array.isArray(c.route) && c.route.length > 1
}

/** The not-chosen candidates that carry a geometry (the chosen one is already drawn). */
export function previewRoutes(choice: RouteChoice, emphasis: string | null): RoutePreview[] {
  return choice.candidates
    .filter((c) => c.label !== choice.chosen && hasRoute(c))
    .map((c) => ({ label: c.label, route: c.route ?? [], emphasis: c.label === emphasis }))
}

/**
 * The line under the candidates: how to see the alternatives, or why some cannot be drawn
 * (records written before the backend stored `candidates[].route`). Null with no alternative.
 */
export function geometryNote(choice: RouteChoice): string | null {
  const alternatives = choice.candidates.filter((c) => c.label !== choice.chosen)
  if (alternatives.length === 0) return null
  const missing = alternatives.filter((c) => !hasRoute(c)).map((c) => c.label)
  if (missing.length === alternatives.length)
    return 'The alternatives were recorded without their geometry, so they cannot be drawn.'
  const hover = 'Hover a route to see it on the map (faint line).'
  if (missing.length === 0) return hover
  const which =
    missing.length === 1 ? `Route ${missing[0]} has` : `Routes ${missing.join(', ')} have`
  return `${hover} ${which} no recorded geometry.`
}

/** Short notes on a candidate: crowding, turns, zones (what the reason compares). */
export function candidateNotes(c: RouteCandidate): string[] {
  const notes: string[] = [`crowd ${c.crowd_index.toFixed(2)}`, `${c.signals} signals`]
  if (c.right_turns > 0) notes.push(`${c.right_turns} right turn${c.right_turns > 1 ? 's' : ''}`)
  if (c.contention_junctions.length > 0)
    notes.push(`${c.contention_junctions.length} contested junction(s)`)
  for (const z of c.incident_zones) notes.push(`${z.type.toLowerCase()} incident zone`)
  if (c.congested_road) notes.push(`busiest: ${c.congested_road}`)
  if (c.source === 'detour') notes.push('detour via point')
  return notes
}
