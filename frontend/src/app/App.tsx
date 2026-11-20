import clsx from 'clsx'
import { useEffect } from 'react'
import { describeError } from '../api/client'
import { useCity } from '../api/queries'
import { useCityStream } from '../api/useCityStream'
import { Toaster } from '../components/ui/Toaster'
import { Spinner } from '../components/ui/Panel'
import { Button } from '../components/ui/Button'
import { AnalyticsView } from '../features/analytics/AnalyticsView'
import { CitySwitcher } from '../features/city-switcher/CitySwitcher'
import { HistoryView } from '../features/history/HistoryView'
import { ConnectionStatus } from '../features/stats/ConnectionStatus'
import { formatHash, parseHash, TABS, useUiStore, type Tab } from '../store/ui'
import { ApiKeyButton } from './ApiKeyButton'
import { LiveView } from './LiveView'

const TAB_LABEL: Record<Tab, string> = { live: 'Live', history: 'History', analytics: 'Analytics' }

/** Keeps `#/{tab}/{city}` and the UI store in sync (deep links, back/forward). */
function useHashRoute() {
  const tab = useUiStore((s) => s.tab)
  const city = useUiStore((s) => s.city)
  useEffect(() => {
    const onHash = () => {
      const r = parseHash(location.hash)
      const s = useUiStore.getState()
      if (r.tab && r.tab !== s.tab) s.setTab(r.tab)
      if (r.city && r.city !== s.city) s.setCity(r.city)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  useEffect(() => {
    const next = formatHash(tab, city)
    if (location.hash !== next) history.replaceState(null, '', next)
  }, [tab, city])
}

export function App() {
  useHashRoute()
  const tab = useUiStore((s) => s.tab)
  const setTab = useUiStore((s) => s.setTab)
  const cityId = useUiStore((s) => s.city)
  const city = useCity(cityId)
  // One live stream for the selected city on every tab: status + sim clock in the header,
  // and incident updates refresh History/Analytics queries too.
  useCityStream(cityId)
  const data = city.data?.id === cityId ? city.data : undefined

  return (
    <div className="flex h-full min-h-0 flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-[3000] focus:rounded focus:bg-rose-600 focus:px-3 focus:py-1 focus:text-white"
      >
        Skip to content
      </a>
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-white/[0.06] bg-ink-950 px-3 py-2 shadow-md">
        <div className="flex items-center gap-2">
          <span
            aria-hidden="true"
            className="flex h-7 w-7 items-center justify-center rounded-md bg-rose-600 text-sm font-black text-white shadow shadow-rose-900/50"
          >
            D
          </span>
          <div className="leading-tight">
            <div className="text-sm font-black tracking-[0.2em] text-white">DIVERT</div>
            <div className="text-[10px] font-medium uppercase tracking-wider text-rose-300/80">
              Green corridor ops
            </div>
          </div>
        </div>
        <nav aria-label="Views">
          <div role="tablist" aria-label="Views" className="flex gap-1">
            {TABS.map((t) => (
              <button
                key={t}
                type="button"
                role="tab"
                id={`tab-${t}`}
                aria-selected={tab === t}
                aria-controls="main"
                onClick={() => setTab(t)}
                className={clsx(
                  'rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400',
                  tab === t
                    ? 'bg-white/10 text-white'
                    : 'text-slate-400 hover:bg-white/5 hover:text-white',
                )}
              >
                {TAB_LABEL[t]}
              </button>
            ))}
          </div>
        </nav>
        <CitySwitcher />
        <div className="ml-auto flex items-center gap-3">
          <ConnectionStatus timeZone={data?.timezone ?? 'Asia/Kolkata'} />
          <ApiKeyButton />
        </div>
      </header>

      <main
        id="main"
        role="tabpanel"
        aria-labelledby={`tab-${tab}`}
        className="min-h-0 flex-1"
        tabIndex={-1}
      >
        {data ? (
          tab === 'live' ? (
            <LiveView key={data.id} city={data} />
          ) : tab === 'history' ? (
            <HistoryView key={data.id} city={data} />
          ) : (
            <AnalyticsView city={data} />
          )
        ) : city.isError ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
            <p className="text-slate-300">
              Could not load {cityId}: {describeError(city.error)}
            </p>
            <Button variant="primary" onClick={() => void city.refetch()}>
              Retry
            </Button>
          </div>
        ) : (
          <div className="flex h-full items-center justify-center">
            <Spinner label={`Loading ${cityId}`} />
          </div>
        )}
      </main>
      <Toaster />
    </div>
  )
}
