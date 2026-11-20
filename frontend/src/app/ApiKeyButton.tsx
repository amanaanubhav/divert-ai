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
        variant="secondary"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
        title="API key for dispatch and cancel"
      >
        <span className="md:hidden">{hasKey ? 'Key set' : 'Key'}</span>
        <span className="hidden md:inline">{hasKey ? 'API key set' : 'API key'}</span>
      </Button>
      {open ? (
        <form
          id={id}
          className="absolute right-0 top-full z-popover mt-1 w-72 max-w-[calc(100vw-1.5rem)] space-y-2 rounded border border-line bg-surface p-3 shadow"
          onSubmit={(e) => {
            e.preventDefault()
            const v = inputRef.current?.value.trim() ?? ''
            setApiKey(v || null)
            setHasKey(getApiKey() !== null)
            setOpen(false)
            toast.info(v ? 'API key saved' : 'API key cleared', 'Stored in this browser only.')
          }}
        >
          <label htmlFor={`${id}-input`} className="block text-sm font-medium text-fg">
            X-API-Key{' '}
            <span className="font-normal text-muted">(only if the server requires one)</span>
          </label>
          <input
            ref={inputRef}
            id={`${id}-input`}
            type="password"
            autoComplete="off"
            defaultValue={getApiKey() ?? ''}
            className="h-8 w-full rounded border border-field bg-surface px-2 text-sm text-fg"
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
