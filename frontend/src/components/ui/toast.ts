import { create } from 'zustand'

export type ToastTone = 'info' | 'success' | 'warning' | 'error'

export interface Toast {
  id: number
  tone: ToastTone
  title: string
  message?: string
  /** Shown small, for support (`X-Request-ID`). */
  meta?: string
}

interface ToastState {
  toasts: Toast[]
  push: (t: Omit<Toast, 'id'>, ttlMs?: number) => number
  dismiss: (id: number) => void
  /** Drop every toast (the app calls this when the tab or city changes). */
  clear: () => void
}

/** How long a toast stays (ms). Short: toasts confirm, they are not a log (see the activity feed). */
export const TOAST_TTL_MS: Readonly<Record<ToastTone, number>> = {
  info: 3500,
  success: 3500,
  warning: 6000,
  error: 10000,
}

/** At most this many toasts at once (oldest dropped first). */
export const MAX_TOASTS = 3

let seq = 0

export const useToasts = create<ToastState>()((set, get) => ({
  toasts: [],
  push: (t, ttlMs = TOAST_TTL_MS[t.tone]) => {
    seq += 1
    const id = seq
    set({ toasts: [...get().toasts.slice(-(MAX_TOASTS - 1)), { id, ...t }] })
    if (ttlMs > 0) setTimeout(() => get().dismiss(id), ttlMs)
    return id
  },
  dismiss: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
  clear: () => {
    if (get().toasts.length > 0) set({ toasts: [] })
  },
}))

export const toast = {
  info: (title: string, message?: string) =>
    useToasts.getState().push({ tone: 'info', title, ...(message ? { message } : {}) }),
  success: (title: string, message?: string) =>
    useToasts.getState().push({ tone: 'success', title, ...(message ? { message } : {}) }),
  warning: (title: string, message?: string) =>
    useToasts.getState().push({ tone: 'warning', title, ...(message ? { message } : {}) }),
  error: (title: string, message?: string, meta?: string) =>
    useToasts
      .getState()
      .push({ tone: 'error', title, ...(message ? { message } : {}), ...(meta ? { meta } : {}) }),
}
