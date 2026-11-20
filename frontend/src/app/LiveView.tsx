import type { CityDetail } from '../api/types'
import { DispatchPanel } from '../features/dispatch/DispatchPanel'
import { FleetPanel } from '../features/fleet/FleetPanel'
import { IncidentsPanel } from '../features/incidents/IncidentsPanel'
import { useLiveIncidents } from '../features/incidents/useLiveIncidents'
import { LiveMap } from '../features/map/LiveMap'
import { StatsPanel } from '../features/stats/StatsPanel'

/** Live tab: dispatch + stats | map | incidents + fleet. Stacks on narrow screens. */
export function LiveView({ city }: { city: CityDetail }) {
  const { incidents, active, isLoading } = useLiveIncidents(city.id)
  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-3 p-3 lg:grid-cols-[19rem,minmax(0,1fr),21rem] lg:grid-rows-[minmax(0,1fr)]">
      <aside
        className="order-2 flex min-h-0 flex-col gap-3 lg:order-1 lg:overflow-y-auto"
        aria-label="Dispatch and statistics"
      >
        <DispatchPanel city={city} />
        <StatsPanel city={city} activeIncidents={active.length} />
      </aside>
      <section
        className="order-1 h-[60vh] min-h-0 lg:order-2 lg:h-auto"
        aria-label={`${city.name} live map`}
      >
        <LiveMap city={city} incidents={active} />
      </section>
      <aside
        className="order-3 flex min-h-0 flex-col gap-3 lg:grid lg:grid-rows-[minmax(0,1fr)_minmax(0,1.2fr)]"
        aria-label="Incidents and fleet"
      >
        <IncidentsPanel city={city} incidents={incidents} isLoading={isLoading} />
        <FleetPanel city={city} />
      </aside>
    </div>
  )
}
