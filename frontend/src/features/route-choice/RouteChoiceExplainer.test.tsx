import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { LatLng, RouteCandidate, RouteChoice } from '../../api/types'
import { RouteChoiceExplainer } from './RouteChoiceExplainer'
import { geometryNote, hasRoute, previewRoutes } from './routeChoice'

/** A simplified candidate polyline as the backend sends it: `candidates[].route`, [[lat, lng]]. */
const ROUTE_A: LatLng[] = [
  [28.6, 77.2],
  [28.605, 77.2],
  [28.61, 77.21],
]
const ROUTE_B: LatLng[] = [
  [28.6, 77.2],
  [28.6, 77.21],
  [28.61, 77.21],
]

function candidate(label: string, route?: LatLng[]): RouteCandidate {
  return {
    label,
    source: 'osrm',
    distance_m: 2000 + label.charCodeAt(0),
    base_time_s: 150,
    congested_time_s: 180,
    junction_delay_s: 6,
    penalties_s: 0,
    cost_s: 186 + label.charCodeAt(0),
    crowd_index: 0.31,
    signals: 2,
    right_turns: 1,
    contention_junctions: [],
    incident_zones: [],
    congested_road: null,
    ...(route ? { route } : {}),
  }
}

function choice(candidates: RouteCandidate[]): RouteChoice {
  return { chosen: 'A', reason: 'Route A: 1.2 min faster than B', tie_break: null, candidates }
}

describe('geometryNote', () => {
  it('invites a hover when every alternative has its geometry', () => {
    const c = choice([candidate('A', ROUTE_A), candidate('B', ROUTE_B)])
    expect(geometryNote(c)).toBe('Hover a route to see it on the map (faint line).')
  })

  it('says the geometry is missing only when no alternative has one (older records)', () => {
    const c = choice([candidate('A'), candidate('B')])
    expect(geometryNote(c)).toMatch(/recorded without their geometry/)
  })

  it('names the alternatives that cannot be drawn when only some can', () => {
    const c = choice([candidate('A', ROUTE_A), candidate('B', ROUTE_B), candidate('C')])
    expect(geometryNote(c)).toBe(
      'Hover a route to see it on the map (faint line). Route C has no recorded geometry.',
    )
  })

  it('treats a one-point route as missing and stays silent with no alternative', () => {
    expect(hasRoute(candidate('B', [[28.6, 77.2]]))).toBe(false)
    expect(geometryNote(choice([candidate('A', ROUTE_A)]))).toBeNull()
  })
})

describe('previewRoutes', () => {
  it('returns the not-chosen candidates with geometry, the hovered one emphasised', () => {
    const c = choice([candidate('A', ROUTE_A), candidate('B', ROUTE_B), candidate('C')])
    expect(previewRoutes(c, 'B')).toEqual([{ label: 'B', route: ROUTE_B, emphasis: true }])
    expect(previewRoutes(c, null)[0]?.emphasis).toBe(false)
  })
})

describe('RouteChoiceExplainer', () => {
  it('draws the alternatives from candidates[].route and does not call them missing', async () => {
    const onPreview = vi.fn()
    render(
      <RouteChoiceExplainer
        choice={choice([candidate('A', ROUTE_A), candidate('B', ROUTE_B)])}
        onPreview={onPreview}
        defaultOpen
      />,
    )
    expect(screen.getByText(/Hover a route to see it on the map/)).toBeInTheDocument()
    expect(screen.queryByText(/cannot be drawn/)).not.toBeInTheDocument()
    await userEvent.hover(screen.getByText('Route B'))
    expect(onPreview).toHaveBeenLastCalledWith([{ label: 'B', route: ROUTE_B, emphasis: true }])
  })

  it('explains that older records cannot be drawn and previews nothing', async () => {
    const onPreview = vi.fn()
    render(
      <RouteChoiceExplainer
        choice={choice([candidate('A'), candidate('B')])}
        onPreview={onPreview}
        defaultOpen
      />,
    )
    expect(screen.getByText(/recorded without their geometry/)).toBeInTheDocument()
    await userEvent.hover(screen.getByText('Route B'))
    expect(onPreview).toHaveBeenLastCalledWith([])
  })
})
