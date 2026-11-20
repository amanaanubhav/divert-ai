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
}

let seq = 0

export const useToasts = create<ToastState>()((set, get) => ({
  toasts: [],
  push: (t, ttlMs = t.tone === 'error' ? 8000 : 4500) => {
    seq += 1
    const id = seq
    set({ toasts: [...get().toasts.slice(-4), { id, ...t }] })
    if (ttlMs > 0) setTimeout(() => get().dismiss(id), ttlMs)
    return id
  },
  dismiss: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
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
