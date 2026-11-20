import clsx from 'clsx'
import { useToasts, type ToastTone } from './toast'

const TONE: Record<ToastTone, string> = {
  info: 'border-sky-500/40',
  success: 'border-emerald-500/50',
  warning: 'border-amber-500/50',
  error: 'border-red-500/60',
}
const DOT: Record<ToastTone, string> = {
  info: 'bg-sky-400',
  success: 'bg-emerald-400',
  warning: 'bg-amber-400',
  error: 'bg-red-500',
}

export function Toaster() {
  const toasts = useToasts((s) => s.toasts)
  const dismiss = useToasts((s) => s.dismiss)
  return (
    <div
      aria-live="polite"
      aria-relevant="additions"
      className="pointer-events-none fixed bottom-4 right-4 z-[2000] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          role={t.tone === 'error' ? 'alert' : 'status'}
          className={clsx(
            'pointer-events-auto flex gap-3 rounded-lg border-l-4 bg-ink-800 px-3 py-2.5 shadow-xl shadow-black/40 ring-1 ring-white/10',
            TONE[t.tone],
          )}
        >
          <span
            aria-hidden="true"
            className={clsx('mt-1.5 h-2 w-2 shrink-0 rounded-full', DOT[t.tone])}
          />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-slate-100">{t.title}</p>
            {t.message ? <p className="mt-0.5 text-[13px] text-slate-300">{t.message}</p> : null}
            {t.meta ? <p className="mt-1 font-mono text-[10px] text-slate-500">{t.meta}</p> : null}
          </div>
          <button
            type="button"
            onClick={() => dismiss(t.id)}
            className="h-6 w-6 shrink-0 rounded text-slate-400 hover:bg-white/10 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
            aria-label="Dismiss notification"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  )
}
