import clsx from 'clsx'
import { useEffect } from 'react'
import { describeError } from '../api/client'
import { useCity } from '../api/queries'
import { useCityStream } from '../api/useCityStream'
import { Toaster } from '../components/ui/Toaster'
import { useToasts } from '../components/ui/toast'
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

  // Toasts belong to the view that raised them: drop them when the view changes.
  useEffect(() => {
    useToasts.getState().clear()
  }, [tab, cityId])

  return (
    <div className="flex h-full min-h-0 flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-skip focus:rounded focus:bg-primary focus:px-3 focus:py-1 focus:text-on-primary"
      >
        Skip to content
      </a>
      {/* `relative z-header`: the header (and its popover) always paint above the content and
          its maps; see lib/zIndex.ts. */}
      <header className="relative z-header flex shrink-0 flex-wrap items-center justify-between gap-x-6 gap-y-1 border-b border-line bg-surface px-3 sm:px-4">
        <div className="flex min-w-0 items-center gap-4 sm:gap-6">
          <div className="py-2 leading-tight">
            <div className="text-base font-semibold text-fg">DIVERT</div>
            <div className="hidden text-xs text-muted sm:block">Emergency dispatch console</div>
          </div>
          <nav aria-label="Views" className="self-stretch">
            <div role="tablist" aria-label="Views" className="flex h-full">
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
                    'focus-inset -mb-px min-h-[2.75rem] border-b-2 px-3 text-sm font-medium',
                    tab === t
                      ? 'border-accent text-fg'
                      : 'border-transparent text-muted hover:border-line hover:text-fg',
                  )}
                >
                  {TAB_LABEL[t]}
                </button>
              ))}
            </div>
          </nav>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-1 py-1.5">
          <CitySwitcher />
          <div className="flex items-center gap-3">
            <ConnectionStatus timeZone={data?.timezone ?? 'Asia/Kolkata'} />
            <ApiKeyButton />
          </div>
        </div>
      </header>

      {/* The content area scrolls on its own (stacked layouts on small screens); the toasts
          are anchored to its top, below the header. */}
      <div className="relative min-h-0 flex-1">
        <main
          id="main"
          role="tabpanel"
          aria-labelledby={`tab-${tab}`}
          className="h-full overflow-y-auto"
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
              <p className="text-fg">
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
        <Toaster className="absolute inset-x-0 top-2" />
      </div>
    </div>
  )
}
