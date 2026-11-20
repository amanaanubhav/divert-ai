import L from 'leaflet'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { CircleMarker, MapContainer, Polyline, Tooltip, useMap } from 'react-leaflet'
import type { IncidentDetail, TrackPoint } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { Segmented } from '../../components/ui/Segmented'
import { formatDuration, formatSpeed } from '../../lib/format'
import { SERVICE_COLOR, SIGNAL_COLOR } from '../../lib/theme'
import { useUiStore } from '../../store/ui'
import { incidentIcon, vehicleIcon } from '../map/icons'
import { BaseTiles } from '../map/MapHelpers'
import { frameAt, prepareReplay, type Replay } from './replay'

const SPEEDS = ['1', '4', '16', '64'] as const
type Speed = (typeof SPEEDS)[number]

/** Recorded route + positions of an incident, with a play / pause / scrub replay. */
export function TrackReplay({
  detail,
  points,
}: {
  detail: IncidentDetail
  points: readonly TrackPoint[]
}) {
  const dark = useUiStore((s) => s.darkTiles)
  const replay = useMemo(
    () => prepareReplay(points, detail.dispatches),
    [points, detail.dispatches],
  )
  const [t, setTState] = useState(0)
  const tRef = useRef(0)
  const setT = (v: number) => {
    tRef.current = v
    setTState(v)
  }
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<Speed>('16')
  const sliderId = useId()
  const dispatch = detail.dispatches[0]

  useEffect(() => {
    if (!playing || !replay) return
    let raf = 0
    let last = performance.now()
    const step = (now: number) => {
      const next = Math.min(replay.durationMs, tRef.current + (now - last) * Number(speed))
      last = now
      tRef.current = next
      setTState(next)
      if (next >= replay.durationMs) {
        setPlaying(false)
        return
      }
      raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [playing, speed, replay])

  const bounds = useMemo(() => {
    const pts: [number, number][] = [detail.location as [number, number]]
    for (const d of detail.dispatches) for (const p of d.route) pts.push(p as [number, number])
    for (const p of points) pts.push(p.pos as [number, number])
    return L.latLngBounds(pts)
  }, [detail, points])

  const frame = replay ? frameAt(replay, t) : null
  const travelled =
    replay && frame
      ? replay.points.slice(0, frame.index + 1).map((p) => p.pos as [number, number])
      : []
  if (frame) travelled.push(frame.pos as [number, number])

  return (
    <div className="space-y-2">
      <div className="h-64 overflow-hidden rounded border border-line sm:h-72">
        <MapContainer
          bounds={bounds}
          boundsOptions={{ padding: [20, 20] }}
          preferCanvas
          className="h-full w-full"
          zoomControl
        >
          <BaseTiles dark={dark} />
          <FitOnChange bounds={bounds} />
          {detail.dispatches.map((d) => (
            <Polyline
              key={d.id}
              positions={d.route as [number, number][]}
              pathOptions={{ color: SERVICE_COLOR[d.service], weight: 4, opacity: 0.35 }}
            />
          ))}
          {(dispatch?.corridor ?? []).map((c) => {
            const pre = detail.preemptions.find((p) => p.signal_id === c.signal_id)
            return (
              <CircleMarker
                key={c.signal_id}
                center={c.pos as [number, number]}
                radius={5}
                pathOptions={{
                  color: '#fff',
                  weight: 1.5,
                  fillOpacity: 1,
                  fillColor: pre ? SIGNAL_COLOR[pre.state] : SIGNAL_COLOR.NORMAL,
                }}
              >
                <Tooltip className="divert-tip">
                  {c.name ?? c.signal_id} · lane {c.lane} ·{' '}
                  {pre ? pre.state.toLowerCase() : 'not pre-empted'}
                </Tooltip>
              </CircleMarker>
            )
          })}
          {travelled.length > 1 && dispatch ? (
            <Polyline
              positions={travelled}
              pathOptions={{ color: SERVICE_COLOR[dispatch.service], weight: 4, opacity: 1 }}
            />
          ) : null}
          <IncidentPin detail={detail} />
          {frame && dispatch ? (
            <ReplayMarker
              replay={replay}
              pos={frame.pos}
              heading={frame.heading}
              service={dispatch.service}
              id={dispatch.vehicle_id}
            />
          ) : null}
        </MapContainer>
      </div>
      {replay && replay.points.length > 1 ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="primary"
            onClick={() => {
              if (!playing && t >= replay.durationMs) setT(0)
              setPlaying(!playing)
            }}
            aria-label={playing ? 'Pause replay' : 'Play replay'}
          >
            {playing ? 'Pause' : 'Play'}
          </Button>
          <label htmlFor={sliderId} className="sr-only">
            Replay position
          </label>
          <input
            id={sliderId}
            type="range"
            min={0}
            max={Math.max(1, Math.round(replay.durationMs))}
            step={100}
            value={Math.round(t)}
            onChange={(e) => {
              setPlaying(false)
              setT(Number(e.target.value))
            }}
            className="min-w-[8rem] flex-1 accent-primary"
            aria-valuetext={`${formatDuration(t / 1000)} of ${formatDuration(replay.durationMs / 1000)}`}
          />
          <span className="text-xs tabular-nums text-muted">
            {formatDuration(t / 1000)} / {formatDuration(replay.durationMs / 1000)}
          </span>
          <Segmented
            label="Replay speed"
            size="sm"
            value={speed}
            onChange={setSpeed}
            options={SPEEDS.map((s) => ({ value: s, label: `${s}×` }))}
          />
          <span className="text-xs tabular-nums text-muted">
            {frame ? formatSpeed(frame.speed_mps) : null}
          </span>
        </div>
      ) : (
        <p className="text-sm text-muted">
          {points.length === 0
            ? 'No recorded positions (retention may have removed them).'
            : 'Only one position sample.'}
        </p>
      )}
    </div>
  )
}

function FitOnChange({ bounds }: { bounds: L.LatLngBounds }) {
  const map = useMap()
  useEffect(() => {
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20], animate: false })
  }, [map, bounds])
  return null
}

function IncidentPin({ detail }: { detail: IncidentDetail }) {
  const map = useMap()
  useEffect(() => {
    const m = L.marker(detail.location as [number, number], {
      icon: incidentIcon(detail.type),
      keyboard: false,
      interactive: false,
    }).addTo(map)
    return () => {
      m.remove()
    }
  }, [map, detail.location, detail.type])
  return null
}

function ReplayMarker({
  replay,
  pos,
  heading,
  service,
  id,
}: {
  replay: Replay | null
  pos: readonly [number, number]
  heading: number
  service: IncidentDetail['dispatches'][number]['service']
  id: string
}) {
  const map = useMap()
  const ref = useRef<{ marker: L.Marker; body: HTMLElement | null } | null>(null)
  useEffect(() => {
    const marker = L.marker(pos as [number, number], {
      icon: vehicleIcon(service, id),
      keyboard: false,
      interactive: false,
      zIndexOffset: 1000,
    }).addTo(map)
    ref.current = {
      marker,
      body: marker.getElement()?.querySelector<HTMLElement>('.veh-body') ?? null,
    }
    return () => {
      marker.remove()
      ref.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- created once per replay; moved below
  }, [map, service, id, replay])
  useEffect(() => {
    const cur = ref.current
    if (!cur) return
    cur.marker.setLatLng(pos as [number, number])
    if (cur.body) cur.body.style.transform = `rotate(${heading.toFixed(1)}deg)`
  }, [pos, heading])
  return null
}
