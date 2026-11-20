import L from 'leaflet'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { CircleMarker, MapContainer, Polyline, Tooltip, useMap } from 'react-leaflet'
import type { IncidentDetail, ServiceType, TrackPoint } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { Segmented } from '../../components/ui/Segmented'
import { edLabel, formatDuration, formatSpeed, hospitalLabel, LEG_LABEL } from '../../lib/format'
import { LEG_COLOR, legColor, legDash, SIGNAL_COLOR } from '../../lib/theme'
import { useUiStore, type RoutePreview } from '../../store/ui'
import { hospitalIcon, incidentIcon, vehicleIcon } from '../map/icons'
import { BaseTiles } from '../map/MapHelpers'
import {
  drawableLegs,
  frameAt,
  legKey,
  legSpans,
  preemptionAt,
  prepareReplay,
  travelledByLeg,
  type LegSpan,
  type Replay,
} from './replay'

const SPEEDS = ['1', '4', '16', '64'] as const
type Speed = (typeof SPEEDS)[number]

/** Recorded legs + positions of an incident, with a play / pause / scrub replay across all legs. */
export function TrackReplay({
  detail,
  points,
  preview = null,
}: {
  detail: IncidentDetail
  points: readonly TrackPoint[]
  /** Route alternatives to draw faintly ("Why this route" hovered). */
  preview?: readonly RoutePreview[] | null
}) {
  const dark = useUiStore((s) => s.darkTiles)
  const legs = useMemo(() => drawableLegs(detail), [detail])
  const replay = useMemo(
    () => prepareReplay(points, detail.dispatches, legs),
    [points, detail.dispatches, legs],
  )
  const spans = useMemo(() => (replay ? legSpans(replay, legs) : []), [replay, legs])
  const serviceOf = useMemo(
    () => new Map(detail.dispatches.map((d) => [d.id, d.service] as const)),
    [detail.dispatches],
  )
  const kindOf = useMemo(
    () => new Map(legs.map((l) => [legKey(l.dispatch_id, l.seq), l.kind] as const)),
    [legs],
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
    for (const l of legs) for (const p of l.route) pts.push(p as [number, number])
    for (const p of points) pts.push(p.pos as [number, number])
    if (detail.hospital) pts.push(detail.hospital.location as [number, number])
    return L.latLngBounds(pts)
  }, [detail, legs, points])

  const frame = replay ? frameAt(replay, t) : null
  const service: ServiceType =
    (frame && serviceOf.get(frame.dispatchId)) ?? dispatch?.service ?? 'AMBULANCE'
  const frameKind = frame ? kindOf.get(legKey(frame.dispatchId, frame.legSeq)) : undefined
  const travelled = replay && frame ? travelledByLeg(replay, frame) : []

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
          {legs.map((l) => (
            <Polyline
              key={legKey(l.dispatch_id, l.seq)}
              positions={l.route as [number, number][]}
              pathOptions={{
                color: legColor(l.kind, serviceOf.get(l.dispatch_id) ?? 'AMBULANCE'),
                weight: 4,
                opacity: l.kind === 'RETURN' ? 0.6 : 0.35,
                ...(legDash(l.kind) ? { dashArray: legDash(l.kind) } : {}),
              }}
            />
          ))}
          {(preview ?? []).map((r) => (
            <Polyline
              key={`alt-${r.label}`}
              positions={r.route as [number, number][]}
              pathOptions={{
                color: LEG_COLOR.RETURN,
                weight: r.emphasis ? 5 : 4,
                opacity: r.emphasis ? 0.85 : 0.5,
                dashArray: '2 7',
                lineCap: 'round',
              }}
            >
              <Tooltip permanent direction="center" className="divert-tip">
                Route {r.label}
              </Tooltip>
            </Polyline>
          ))}
          {legs.flatMap((l) =>
            (l.corridor ?? []).map((c) => {
              const pre = preemptionAt(detail.preemptions, l.dispatch_id, l.seq, c.signal_id)
              const freeLeft = c.free_left === true
              return (
                <CircleMarker
                  key={`${legKey(l.dispatch_id, l.seq)}:${c.signal_id}:${c.pass_no ?? 0}`}
                  center={c.pos as [number, number]}
                  radius={5}
                  pathOptions={
                    freeLeft
                      ? {
                          color: SIGNAL_COLOR.PREEMPTED,
                          weight: 2,
                          dashArray: '2 2',
                          fillColor: '#fff',
                          fillOpacity: 1,
                        }
                      : {
                          color: '#fff',
                          weight: 1.5,
                          fillOpacity: 1,
                          fillColor: pre ? SIGNAL_COLOR[pre.state] : SIGNAL_COLOR.NORMAL,
                        }
                  }
                >
                  <Tooltip className="divert-tip">
                    {c.name ?? c.signal_id} · {LEG_LABEL[l.kind].toLowerCase()} leg ·{' '}
                    {freeLeft
                      ? 'free left (not claimed)'
                      : `lane ${c.advice ? `${c.advice.lane_index} of ${c.advice.lanes_total}` : c.lane} · ${
                          pre
                            ? pre.state === 'CONFLICT'
                              ? 'queued'
                              : 'pre-empted'
                            : 'not pre-empted'
                        }`}
                  </Tooltip>
                </CircleMarker>
              )
            }),
          )}
          {travelled.map((seg) => (
            <Polyline
              key={`trav-${seg.key}`}
              positions={seg.pts as [number, number][]}
              pathOptions={{
                color: legColor(kindOf.get(seg.key), serviceOf.get(seg.dispatchId) ?? service),
                weight: 4,
                opacity: 1,
                ...(legDash(kindOf.get(seg.key))
                  ? { dashArray: legDash(kindOf.get(seg.key)) }
                  : {}),
              }}
            />
          ))}
          <IncidentPin detail={detail} />
          <HospitalPin detail={detail} />
          {frame && dispatch ? (
            <ReplayMarker
              replay={replay}
              pos={frame.pos}
              heading={frame.heading}
              service={service}
              id={dispatch.vehicle_id}
              patient={frameKind === 'TRANSPORT'}
            />
          ) : null}
        </MapContainer>
      </div>
      {replay && replay.points.length > 1 ? (
        <>
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
          </div>
          <LegBar
            spans={spans}
            durationMs={replay.durationMs}
            service={service}
            current={frame ? legKey(frame.dispatchId, frame.legSeq) : null}
            onJump={(ms) => {
              setPlaying(false)
              setT(ms)
            }}
          />
          <p className="text-xs text-muted" aria-live="off">
            {frameKind ? `${LEG_LABEL[frameKind]} leg` : 'Leg'}
            {frame ? ` · ${formatSpeed(frame.speed_mps)}` : null} · replay in wall-clock time
            (dwells show as stops)
          </p>
        </>
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

/** The legs on the replay time axis, in their line style; each segment jumps to its leg. */
function LegBar({
  spans,
  durationMs,
  service,
  current,
  onJump,
}: {
  spans: readonly LegSpan[]
  durationMs: number
  service: ServiceType
  current: string | null
  onJump: (ms: number) => void
}) {
  if (spans.length === 0) return null
  const total = Math.max(1, durationMs)
  return (
    <div className="space-y-1">
      <div className="relative h-2 rounded-sm bg-subtle" aria-hidden="true">
        {spans.map((s) => (
          <span
            key={legKey(s.dispatchId, s.seq)}
            className="absolute inset-y-0"
            style={{
              left: `${(s.startMs / total) * 100}%`,
              width: `${Math.max(0.8, ((s.endMs - s.startMs) / total) * 100)}%`,
              background: legColor(s.kind ?? undefined, service),
              opacity: s.kind === 'RETURN' ? 0.6 : 1,
            }}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-1" role="group" aria-label="Jump to a leg">
        {spans.map((s) => {
          const key = legKey(s.dispatchId, s.seq)
          return (
            <Button
              key={key}
              size="sm"
              variant={current === key ? 'primary' : 'secondary'}
              aria-pressed={current === key}
              onClick={() => onJump(s.startMs)}
            >
              {s.kind ? LEG_LABEL[s.kind] : `Leg ${s.seq}`}{' '}
              <span className="tabular-nums opacity-80">{formatDuration(s.startMs / 1000)}</span>
            </Button>
          )
        })}
      </div>
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

function HospitalPin({ detail }: { detail: IncidentDetail }) {
  const map = useMap()
  const h = detail.hospital
  useEffect(() => {
    if (!h) return
    const m = L.marker(h.location as [number, number], { icon: hospitalIcon(), keyboard: false })
      .bindTooltip(`Hospital: ${hospitalLabel(h)} · ${edLabel(h.emergency)}`, {
        className: 'divert-tip',
        direction: 'top',
      })
      .addTo(map)
    return () => {
      m.remove()
    }
  }, [map, h])
  return null
}

function ReplayMarker({
  replay,
  pos,
  heading,
  service,
  id,
  patient,
}: {
  replay: Replay | null
  pos: readonly [number, number]
  heading: number
  service: ServiceType
  id: string
  patient: boolean
}) {
  const map = useMap()
  const ref = useRef<{
    marker: L.Marker
    root: HTMLElement | null
    body: HTMLElement | null
  } | null>(null)
  useEffect(() => {
    const marker = L.marker(pos as [number, number], {
      icon: vehicleIcon(service, id),
      keyboard: false,
      interactive: false,
      zIndexOffset: 1000,
    }).addTo(map)
    const root = marker.getElement()?.querySelector<HTMLElement>('.veh') ?? null
    ref.current = { marker, root, body: root?.querySelector<HTMLElement>('.veh-body') ?? null }
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
  useEffect(() => {
    ref.current?.root?.classList.toggle('has-patient', patient)
  }, [patient, replay])
  return null
}
