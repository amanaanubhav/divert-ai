import clsx from 'clsx'
import { useToasts, type ToastTone } from './toast'

const EDGE: Record<ToastTone, string> = {
  info: 'border-l-info',
  success: 'border-l-ok',
  warning: 'border-l-warn',
  error: 'border-l-bad',
}

const TONE_LABEL: Record<ToastTone, string> = {
  info: 'Info',
  success: 'Done',
  warning: 'Warning',
  error: 'Error',
}

/**
 * Notifications, stacked at the top centre of the content area (below the header, so tabs,
 * city switcher and API key are never covered). A toast never blocks a click: the card lets
 * clicks through to whatever is underneath and only its close button takes the pointer.
 * Lifetimes are short (`TOAST_TTL_MS`) and the app clears toasts when the tab or city changes.
 */
export function Toaster({ className }: { className?: string }) {
  const toasts = useToasts((s) => s.toasts)
  const dismiss = useToasts((s) => s.dismiss)
  return (
    <div
      aria-live="polite"
      aria-relevant="additions"
      className={clsx(
        'pointer-events-none z-toast flex flex-col items-center gap-2 px-3',
        className ?? 'fixed inset-x-0 top-2',
      )}
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          role={t.tone === 'error' ? 'alert' : 'status'}
          className={clsx(
            'pointer-events-none flex w-full max-w-[26rem] items-start gap-2 rounded border border-l-4 border-line bg-surface py-2 pl-3 pr-1.5 shadow',
            EDGE[t.tone],
          )}
        >
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-fg">
              <span className="sr-only">{TONE_LABEL[t.tone]}: </span>
              {t.title}
            </p>
            {t.message ? <p className="mt-0.5 text-sm text-muted">{t.message}</p> : null}
            {t.meta ? <p className="mt-1 font-mono text-xs text-muted">{t.meta}</p> : null}
          </div>
          <button
            type="button"
            onClick={() => dismiss(t.id)}
            className="pointer-events-auto -my-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-base leading-none text-muted hover:bg-subtle hover:text-fg"
            aria-label="Dismiss notification"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  )
}
