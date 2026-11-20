import clsx from 'clsx'
import type { RouteChoice } from '../../api/types'
import { formatDistance, formatDuration } from '../../lib/format'
import type { RoutePreview } from '../../store/ui'
import { candidateNotes, geometryNote, previewRoutes } from './routeChoice'

/**
 * "Why this route": the reason, then every candidate with its distance, travel time and cost.
 * Hovering (or focusing) it previews the alternatives on the map through `onPreview`.
 */
export function RouteChoiceExplainer({
  choice,
  onPreview,
  defaultOpen = false,
}: {
  choice: RouteChoice
  onPreview?: (routes: RoutePreview[] | null) => void
  defaultOpen?: boolean
}) {
  const note = geometryNote(choice)
  const alternatives = choice.candidates.length - 1
  const preview = (emphasis: string | null) => onPreview?.(previewRoutes(choice, emphasis))
  const clear = () => onPreview?.(null)
  const sorted = [...choice.candidates].sort((a, b) =>
    a.label === choice.chosen ? -1 : b.label === choice.chosen ? 1 : a.cost_s - b.cost_s,
  )
  return (
    <details
      className="group rounded border border-line"
      open={defaultOpen}
      onMouseEnter={() => preview(null)}
      onMouseLeave={clear}
      onFocus={() => preview(null)}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) clear()
      }}
    >
      <summary className="cursor-pointer select-none px-2 py-1 text-xs font-medium text-accent">
        Why this route
        <span className="font-normal text-muted">
          {' '}
          ·{' '}
          {alternatives === 0
            ? 'only route'
            : `${alternatives} alternative${alternatives > 1 ? 's' : ''}`}
        </span>
      </summary>
      <div className="space-y-2 border-t border-line px-2 py-2 text-xs">
        <p className="text-fg">{choice.reason}</p>
        {choice.tie_break ? (
          <p className="text-muted">Costs within 5 %: decided by {choice.tie_break}.</p>
        ) : null}
        <ul className="divide-y divide-line" aria-label="Route candidates">
          {sorted.map((c) => {
            const chosen = c.label === choice.chosen
            return (
              <li
                key={c.label}
                className={clsx('py-1', chosen && 'font-medium')}
                onMouseEnter={() => preview(c.label)}
                onMouseLeave={() => preview(null)}
              >
                <div className="grid grid-cols-[3.5rem,1fr,auto] items-baseline gap-2 tabular-nums">
                  <span className="text-fg">
                    Route {c.label}
                    {chosen ? <span className="sr-only"> (chosen)</span> : null}
                  </span>
                  <span className="text-fg">
                    {formatDistance(c.distance_m)} · {formatDuration(c.congested_time_s)}
                  </span>
                  <span
                    className={chosen ? 'text-ok' : 'text-muted'}
                    title="Cost = predicted travel time + junction delays + penalties"
                  >
                    cost {formatDuration(c.cost_s)}
                    {chosen ? ' · chosen' : ''}
                  </span>
                </div>
                <div className="font-normal text-muted">{candidateNotes(c).join(' · ')}</div>
              </li>
            )
          })}
        </ul>
        {note ? <p className="text-muted">{note}</p> : null}
      </div>
    </details>
  )
}
