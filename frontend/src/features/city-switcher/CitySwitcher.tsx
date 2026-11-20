import { useCities } from '../../api/queries'
import { CITY_IDS } from '../../api/types'
import { Segmented } from '../../components/ui/Segmented'
import { useUiStore } from '../../store/ui'

const FALLBACK_NAMES: Record<string, string> = {
  delhi: 'Delhi',
  kolkata: 'Kolkata',
  bengaluru: 'Bengaluru',
}

/** Delhi / Kolkata / Bengaluru. Switching refits the map, reloads reference data and reconnects. */
export function CitySwitcher() {
  const cities = useCities()
  const city = useUiStore((s) => s.city)
  const setCity = useUiStore((s) => s.setCity)
  const options = (cities.data?.cities.map((c) => ({ value: c.id, label: c.name })) ??
    CITY_IDS.map((id) => ({ value: id, label: FALLBACK_NAMES[id] ?? id }))) as {
    value: string
    label: string
  }[]
  return <Segmented label="City" value={city} options={options} onChange={setCity} size="sm" />
}
