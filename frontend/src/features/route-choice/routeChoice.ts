import type { RouteCandidate, RouteChoice } from '../../api/types'
import type { RoutePreview } from '../../store/ui'

/** The not-chosen candidates that carry a geometry (the chosen one is already drawn). */
export function previewRoutes(choice: RouteChoice, emphasis: string | null): RoutePreview[] {
  return choice.candidates
    .filter((c) => c.label !== choice.chosen && c.route && c.route.length > 1)
    .map((c) => ({ label: c.label, route: c.route ?? [], emphasis: c.label === emphasis }))
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
