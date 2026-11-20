import { useEffect, useMemo } from 'react'
import { useIncidentDetail } from '../../api/queries'
import { legRouteChoice } from './fleetText'
import { Spinner } from '../../components/ui/Panel'
import type { LiveVehicle } from '../../store/live'
import { useUiStore } from '../../store/ui'
import { RouteChoiceExplainer } from '../route-choice/RouteChoiceExplainer'

/** "Why this route" for the selected live vehicle; hovering it previews the alternatives. */
export function LiveRouteWhy({ v }: { v: LiveVehicle }) {
  const detail = useIncidentDetail(v.incident_id)
  const setPreview = useUiStore((s) => s.setRoutePreview)
  const seq = v.leg?.seq ?? 0
  const choice = useMemo(
    () => (detail.data ? legRouteChoice(detail.data, v.dispatch_id, seq) : null),
    [detail.data, v.dispatch_id, seq],
  )
  const { refetch } = detail
  const missing = detail.data !== undefined && choice === null
  useEffect(() => {
    // a new leg started after the detail was fetched: its row is written before it starts
    if (missing) void refetch()
  }, [missing, seq, refetch])
  useEffect(() => () => setPreview(null), [setPreview])

  if (detail.isLoading) return <Spinner label="Loading route choice" />
  if (!choice) return null
  return <RouteChoiceExplainer choice={choice} onPreview={setPreview} />
}
