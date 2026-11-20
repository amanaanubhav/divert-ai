import type { CityDetail } from '../api/types'
import { DispatchPanel } from '../features/dispatch/DispatchPanel'
import { FleetPanel } from '../features/fleet/FleetPanel'
import { IncidentsPanel } from '../features/incidents/IncidentsPanel'
import { useLiveIncidents } from '../features/incidents/useLiveIncidents'
import { LiveMap } from '../features/map/LiveMap'
import { StatsPanel } from '../features/stats/StatsPanel'

/**
 * Live tab: dispatch + stats | map | incidents + fleet.
 *
 * From `lg` the three columns fill the content area and each side column scrolls on its own.
 * Below `lg` the panels stack at their natural height (map first) and the content area
 * scrolls: nothing is squeezed to the viewport height, so panels never overlap each other.
 */
export function LiveView({ city }: { city: CityDetail }) {
  const { incidents, active, isLoading } = useLiveIncidents(city.id)
  return (
    <div className="grid grid-cols-1 gap-3 p-3 lg:h-full lg:min-h-0 lg:grid-cols-[17rem,minmax(0,1fr),19rem] lg:grid-rows-[minmax(0,1fr)] xl:grid-cols-[19rem,minmax(0,1fr),21rem]">
      <aside
        className="order-2 flex flex-col gap-3 lg:order-1 lg:min-h-0 lg:overflow-y-auto"
        aria-label="Dispatch and statistics"
      >
        <DispatchPanel city={city} />
        <StatsPanel city={city} activeIncidents={active.length} />
      </aside>
      <section
        className="order-1 h-[55vh] min-h-[18rem] lg:order-2 lg:h-auto lg:min-h-0"
        aria-label={`${city.name} live map`}
      >
        <LiveMap city={city} incidents={active} />
      </section>
      <aside
        className="order-3 flex flex-col gap-3 lg:grid lg:min-h-0 lg:grid-rows-[minmax(0,1fr)_minmax(0,1.2fr)]"
        aria-label="Incidents and fleet"
      >
        <IncidentsPanel city={city} incidents={incidents} isLoading={isLoading} />
        <FleetPanel city={city} />
      </aside>
    </div>
  )
}
