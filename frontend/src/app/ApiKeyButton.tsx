import { useId, useRef, useState } from 'react'
import { getApiKey, setApiKey } from '../config'
import { Button } from '../components/ui/Button'
import { toast } from '../components/ui/toast'

/** Optional `X-API-Key` for POST/DELETE when the backend sets DIVERT_API_KEY. */
export function ApiKeyButton() {
  const [open, setOpen] = useState(false)
  const [hasKey, setHasKey] = useState(() => getApiKey() !== null)
  const inputRef = useRef<HTMLInputElement>(null)
  const id = useId()
  return (
    <div className="relative">
      <Button
        size="sm"
        variant="ghost"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
        title="API key for dispatch and cancel"
      >
        <span aria-hidden="true">⚿</span>
        <span className="hidden md:inline">{hasKey ? 'Key set' : 'API key'}</span>
      </Button>
      {open ? (
        <form
          id={id}
          className="absolute right-0 top-9 z-[1600] w-72 space-y-2 rounded-lg bg-ink-800 p-3 shadow-xl ring-1 ring-white/10"
          onSubmit={(e) => {
            e.preventDefault()
            const v = inputRef.current?.value.trim() ?? ''
            setApiKey(v || null)
            setHasKey(getApiKey() !== null)
            setOpen(false)
            toast.info(v ? 'API key saved' : 'API key cleared', 'Stored in this browser only.')
          }}
        >
          <label htmlFor={`${id}-input`} className="block text-xs font-medium text-slate-300">
            X-API-Key (only if the server requires one)
          </label>
          <input
            ref={inputRef}
            id={`${id}-input`}
            type="password"
            autoComplete="off"
            defaultValue={getApiKey() ?? ''}
            className="w-full rounded-md border-0 bg-ink-900 px-2 py-1.5 text-sm text-slate-100 ring-1 ring-inset ring-white/10 focus:ring-2 focus:ring-sky-400"
          />
          <div className="flex justify-end gap-2">
            <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
              Close
            </Button>
            <Button size="sm" variant="primary" type="submit">
              Save
            </Button>
          </div>
        </form>
      ) : null}
    </div>
  )
}
