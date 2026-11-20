#!/usr/bin/env node
// UI clickability check (dev tool, not shipped): drives a real browser over every view, city and
// viewport and asserts that nothing covers a visible control, i.e. that
// document.elementFromPoint(centre of the control) is the control itself or inside it, and that
// the control's whole bounding box can be shown: inside the viewport and inside every scrolling
// panel that holds it (scrolled into view if needed).
//
// Layout checks in every state: the page never scrolls sideways, no scrolling panel overflows
// sideways (only boxes marked `data-scroll-x`, e.g. wide tables, may), every panel lies inside
// the viewport and inside the column / panel that holds it, and nothing sticks out of a panel.
// The mocks carry very long real-style station and junction names to stress this.
//
// It also checks the same after a dispatch toast, with the History drawer open and with the
// API-key popover open. Screenshots and results.json go to .ui-check/ (gitignored).
//
// Usage:
//   npm run check:ui                       # mock mode: serves dist/ with `vite preview`, API + WebSocket mocked
//   npm run check:ui -- --url http://localhost:8080   # against the running stack (POST/DELETE still mocked)
// Options:
//   --cities delhi,kolkata   --viewports 1440x900,360x740   --real-tiles   --headed
//   --browser msedge|chrome|chromium (default msedge, then chrome, then playwright's chromium)
//   --dark (emulate prefers-color-scheme: dark)
//   --mock-api (with --url: also mock every GET and the WebSocket, e.g. while the backend is down)
//   --out .ui-check          --keep-going (exit 0 even with failures)
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync, existsSync } from 'node:fs'
import { deflateSync } from 'node:zlib'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { answer, CITY_IDS, streamHandler } from './ui-check-mocks.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const VIEWPORTS = ['360x740', '768x1024', '1024x768', '1280x800', '1440x900', '1920x1080']

function parseArgs(argv) {
  const o = {
    url: null,
    cities: CITY_IDS,
    viewports: VIEWPORTS,
    realTiles: false,
    headed: false,
    browser: null,
    out: join(ROOT, '.ui-check'),
    keepGoing: false,
    dark: false,
    mockApi: false,
  }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    const next = () => argv[++i]
    if (a === '--url') o.url = next().replace(/\/$/, '')
    else if (a === '--cities') o.cities = next().split(',')
    else if (a === '--viewports') o.viewports = next().split(',')
    else if (a === '--real-tiles') o.realTiles = true
    else if (a === '--headed') o.headed = true
    else if (a === '--browser') o.browser = next()
    else if (a === '--out') o.out = resolve(next())
    else if (a === '--keep-going') o.keepGoing = true
    else if (a === '--dark') o.dark = true
    else if (a === '--mock-api') o.mockApi = true
    else throw new Error(`unknown option ${a}`)
  }
  return o
}

// ---------------------------------------------------------------- tiny PNG for stubbed tiles
function crc32(buf) {
  let c = ~0
  for (const b of buf) {
    c ^= b
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1))
  }
  return ~c >>> 0
}
function chunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const td = Buffer.concat([Buffer.from(type), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(td))
  return Buffer.concat([len, td, crc])
}
function solidPng(r, g, b) {
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(1, 0)
  ihdr.writeUInt32BE(1, 4)
  ihdr[8] = 8 // bit depth
  ihdr[9] = 2 // truecolour
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(Buffer.from([0, r, g, b]))),
    chunk('IEND', Buffer.alloc(0)),
  ])
}
const TILE = solidPng(0xe9, 0xe7, 0xe2)

// ---------------------------------------------------------------- browser + server
async function launch(o) {
  const { chromium } = await import('playwright-core')
  const tries = o.browser ? [o.browser] : ['msedge', 'chrome', 'chromium']
  let last
  for (const channel of tries) {
    try {
      const opts = { headless: !o.headed }
      if (channel !== 'chromium') opts.channel = channel
      const b = await chromium.launch(opts)
      return { browser: b, channel }
    } catch (e) {
      last = e
    }
  }
  throw new Error(
    `No browser found (tried ${tries.join(', ')}). Install Microsoft Edge or Chrome, or run ` +
      `"npx playwright-core install chromium".\n${last?.message ?? ''}`,
  )
}

async function waitForHttp(url, ms) {
  const end = Date.now() + ms
  while (Date.now() < end) {
    try {
      const r = await fetch(url)
      if (r.ok) return
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 200))
  }
  throw new Error(`timed out waiting for ${url}`)
}

async function startPreview() {
  if (!existsSync(join(ROOT, 'dist', 'index.html')))
    throw new Error(
      'dist/ is missing: run `npm run build` first (or pass --url http://localhost:8080)',
    )
  const port = 4319
  const vite = join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js')
  const child = spawn(process.execPath, [vite, 'preview', '--port', String(port), '--strictPort'], {
    cwd: ROOT,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let log = ''
  child.stdout.on('data', (d) => (log += d))
  child.stderr.on('data', (d) => (log += d))
  const base = `http://localhost:${port}`
  try {
    await waitForHttp(`${base}/index.html`, 20_000)
  } catch (e) {
    child.kill()
    throw new Error(`vite preview did not start:\n${log}\n${e.message}`, { cause: e })
  }
  return { base, stop: () => child.kill() }
}

// ---------------------------------------------------------------- in-page hit testing
/**
 * Runs in the page. Layout first (see layoutFailures below), then for every visible control
 * (optionally limited to `scope` roots):
 *  pass 1: controls fully inside their scroll containers and the viewport are hit-tested where
 *          they are (what the operator sees right now);
 *  pass 2: the rest are scrolled into view (block: centre, inline: nearest) and must then be
 *          fully visible (bounding box inside the viewport and every clipping panel) and
 *          hit-tested; a control that cannot be brought into view is reported as unreachable,
 *          one that stays partly cut off as clipped.
 */
function hitTest(scopeSelectors) {
  // Sub-pixel rounding allowance for box comparisons (px).
  const TOL = 1
  // Boxes that may scroll sideways on purpose (wide tables); Leaflet's popup content scrolls
  // sideways on a small map rather than being clipped (see index.css).
  const SCROLL_X_OK = '[data-scroll-x], .leaflet-popup-content'
  // What counts as a panel: page columns, titled sections, cards, the drawer, map popups.
  const PANEL =
    'main, aside, section, article, header, [role="dialog"], .leaflet-popup-content-wrapper'
  const SEL = [
    'a[href]',
    'button',
    'input:not([type="hidden"])',
    'select',
    'textarea',
    'summary',
    '[role="tab"]',
    '[role="radio"]',
    '[role="button"]',
    '[role="checkbox"]',
    '[role="switch"]',
    '[role="link"]',
    '[role="menuitem"]',
  ].join(',')
  const vw = window.innerWidth
  const vh = window.innerHeight
  const roots = scopeSelectors
    ? scopeSelectors.flatMap((s) => [...document.querySelectorAll(s)])
    : [document]
  const all = new Set()
  for (const r of roots) {
    if (r !== document && r.matches?.(SEL)) all.add(r)
    for (const el of r.querySelectorAll(SEL)) all.add(el)
  }

  const describe = (el) => {
    if (!el) return 'nothing'
    const tag = el.tagName.toLowerCase()
    const id = el.id ? `#${el.id}` : ''
    const role = el.getAttribute('role') ? `[role=${el.getAttribute('role')}]` : ''
    const cls =
      typeof el.className === 'string' && el.className
        ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.')
        : ''
    const name = (
      el.getAttribute('aria-label') ||
      el.getAttribute('title') ||
      el.textContent ||
      el.getAttribute('placeholder') ||
      el.value ||
      ''
    )
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 40)
    return `${tag}${id}${role}${cls}${name ? ` "${name}"` : ''}`
  }
  const skip = (el) =>
    el.closest(
      '.leaflet-marker-pane, .leaflet-tooltip-pane, .leaflet-popup-pane, .leaflet-shadow-pane',
    ) || el.matches('.leaflet-container')
  const visible = (el) => {
    if (el.closest('[aria-hidden="true"], [hidden], [inert]')) return false
    const cs = getComputedStyle(el)
    if (cs.display === 'none' || cs.visibility !== 'visible') return false
    for (let e = el; e; e = e.parentElement)
      if (Number(getComputedStyle(e).opacity) === 0) return false
    const r = el.getBoundingClientRect()
    if (r.width < 4 || r.height < 4) return false // sr-only (1 px) and similar
    if (r.width * r.height > vw * vh * 0.8) return false // full-screen backdrops
    return true
  }
  const visibleRect = (el) => {
    const r = el.getBoundingClientRect()
    let { left, top, right, bottom } = r
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const cs = getComputedStyle(a)
      if (cs.position === 'fixed') break
      if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
        const ar = a.getBoundingClientRect()
        const l = ar.left + a.clientLeft
        const t = ar.top + a.clientTop
        left = Math.max(left, l)
        top = Math.max(top, t)
        right = Math.min(right, l + a.clientWidth)
        bottom = Math.min(bottom, t + a.clientHeight)
      }
    }
    left = Math.max(left, 0)
    top = Math.max(top, 0)
    right = Math.min(right, vw)
    bottom = Math.min(bottom, vh)
    return { r, left, top, right, bottom, w: right - left, h: bottom - top }
  }
  const fully = (v) => v.w >= v.r.width - 1 && v.h >= v.r.height - 1 && v.w > 0 && v.h > 0
  const probe = (el, v) => {
    const x = v.left + v.w / 2
    const y = v.top + v.h / 2
    const hit = document.elementFromPoint(x, y)
    if (hit && (hit === el || el.contains(hit))) return null
    if (hit && el.labels && [...el.labels].some((l) => l === hit || l.contains(hit))) return null
    return { x: Math.round(x), y: Math.round(y), hit: describe(hit) }
  }

  // ------------------------------------------------------------ layout (no clicking)
  const clips = (el) => {
    const cs = getComputedStyle(el)
    return cs.overflowX !== 'visible' || cs.overflowY !== 'visible'
  }
  const scrolls = (el) => /^(auto|scroll)$/.test(getComputedStyle(el).overflowX)
  /** Inside the map (tiles, markers, labels): Leaflet positions and clips those itself. */
  const inMap = (el) => Boolean(el.closest('.leaflet-container')) && !el.closest('.leaflet-popup')
  /** The panel or clipping box an element must stay inside; null = the viewport. */
  const holderOf = (el) => {
    if (getComputedStyle(el).position === 'fixed') return null
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      if (getComputedStyle(a).position === 'fixed') return null
      if (a.matches(PANEL) || clips(a)) return a
    }
    return null
  }
  /** Horizontal extent available inside a holder (its padding box when it clips). */
  const spanOf = (h) => {
    if (!h) return { left: 0, right: document.documentElement.clientWidth }
    const r = h.getBoundingClientRect()
    if (!clips(h)) return { left: r.left, right: r.right }
    const left = r.left + h.clientLeft
    return { left, right: left + h.clientWidth }
  }
  const layoutFailures = () => {
    const out = []
    /** Boxes reported as sticking out: their subtrees are not reported again. */
    const stuckOut = []
    const fail = (el, reason, r) =>
      out.push({ control: describe(el), reason, x: Math.round(r.right), y: Math.round(r.top) })
    const doc = document.documentElement
    if (doc.scrollWidth > doc.clientWidth + TOL)
      fail(doc, `page scrolls sideways by ${doc.scrollWidth - doc.clientWidth} px`, {
        right: doc.clientWidth,
        top: 0,
      })
    // The app shell is exactly one viewport tall and #main scrolls inside it; a taller document
    // (e.g. an absolutely positioned sr-only label escaping its scrolling panel) lets focus or
    // the wheel scroll the header away.
    if (doc.scrollHeight > doc.clientHeight + TOL)
      fail(doc, `page scrolls vertically by ${doc.scrollHeight - doc.clientHeight} px`, {
        right: doc.clientWidth,
        top: doc.clientHeight,
      })
    const elements = new Set()
    for (const r of roots) {
      if (r !== document) elements.add(r)
      for (const el of r.querySelectorAll('*')) elements.add(el)
    }
    let checked = 0
    for (const el of elements) {
      if (inMap(el) || el.parentElement?.closest(SCROLL_X_OK)) continue
      if (el instanceof SVGElement && !(el instanceof SVGSVGElement)) continue
      const r = el.getBoundingClientRect()
      if (r.width === 0 && r.height === 0) continue // display: none / contents
      if (stuckOut.some((p) => p.contains(el))) continue
      checked++
      if (scrolls(el) && !el.matches(SCROLL_X_OK) && el.scrollWidth > el.clientWidth + TOL)
        fail(el, `scrolls sideways: content ${el.scrollWidth - el.clientWidth} px too wide`, r)
      const holder = holderOf(el)
      // a map popup must fit its map; otherwise the children of a clipping box are its own
      // business: scroll boxes are checked just above, overflow: hidden clips on purpose
      // (ellipsis, progress bars, the map)
      const mustFit = el.matches('.leaflet-popup-content-wrapper')
      if (!mustFit && holder && clips(holder) && (scrolls(holder) || !holder.matches(PANEL)))
        continue
      const s = spanOf(holder)
      const over = Math.max(s.left - r.left, r.right - s.right)
      if (over > TOL) {
        stuckOut.push(el)
        fail(
          el,
          `sticks out of ${holder ? describe(holder) : 'the viewport'} by ${Math.round(over)} px`,
          r,
        )
      }
    }
    return { checked, failures: out }
  }
  const layout = layoutFailures()

  const candidates = [...all].filter((el) => !skip(el) && visible(el))
  const failures = [...layout.failures]
  const later = []
  for (const el of candidates) {
    const v = visibleRect(el)
    if (!fully(v)) {
      later.push(el)
      continue
    }
    const f = probe(el, v)
    if (f) failures.push({ control: describe(el), reason: 'covered', ...f })
  }
  for (const el of later) {
    el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' })
    const v = visibleRect(el)
    if (v.w <= 0 || v.h <= 0) {
      const r = el.getBoundingClientRect()
      failures.push({
        control: describe(el),
        reason: 'unreachable (cannot be scrolled into view)',
        x: Math.round(r.left),
        y: Math.round(r.top),
        hit: 'off-screen',
      })
      continue
    }
    if (!fully(v)) {
      // part of the box stays outside the viewport or a clipping panel even when scrolled to
      const cut = Math.round(v.r.width - v.w + (v.r.height - v.h))
      failures.push({
        control: describe(el),
        reason: `clipped: ${cut} px of the control stay outside the viewport or its panel`,
        x: Math.round(v.r.left),
        y: Math.round(v.r.top),
        hit: null,
      })
      continue
    }
    const f = probe(el, v)
    if (f)
      failures.push({ control: describe(el), reason: 'covered (after scrolling into view)', ...f })
  }
  // restore scroll so screenshots stay comparable
  window.scrollTo(0, 0)
  for (const el of document.querySelectorAll('*'))
    if (el.scrollTop || el.scrollLeft) {
      el.scrollTop = 0
      el.scrollLeft = 0
    }
  return { checked: candidates.length, layoutChecked: layout.checked, failures }
}

// ---------------------------------------------------------------- scenario helpers
/** A state the live data does not have right now (live mode only). */
class SkipState extends Error {}

const TOAST_REGION = '[aria-live="polite"]:has([role="status"], [role="alert"])'

/** Set from --real-tiles: wait for real basemap tiles before screenshots. */
let REAL_TILES = false

async function settle(page) {
  await page.evaluate(() => document.fonts.ready.then(() => undefined))
  if (REAL_TILES && (await page.locator('.leaflet-container').count()) > 0) {
    await page
      .waitForFunction(() => document.querySelectorAll('.leaflet-tile-loaded').length >= 6, null, {
        timeout: 10_000,
      })
      .catch(() => undefined)
    await page.waitForTimeout(600)
  }
  await page.waitForTimeout(250)
}

async function waitReady(page, view) {
  await page.waitForSelector('#main', { timeout: 15_000 })
  const markers = {
    live: '#dispatch-title',
    history: '#history-table-title',
    analytics: '#an-kpi-title, h1',
  }
  await page.waitForSelector(markers[view], { timeout: 15_000 })
  await page.waitForFunction(
    () => {
      const main = document.querySelector('#main')
      return main && !/Loading/.test(main.textContent ?? '')
    },
    null,
    { timeout: 15_000 },
  )
  await settle(page)
}

async function dispatchFromMap(page) {
  const map = page.locator('.leaflet-container').first()
  await map.scrollIntoViewIfNeeded()
  const box = await map.boundingBox()
  if (!box) throw new Error('live map not found')
  const submit = page.locator('form button[type="submit"]').first()
  const offsets = [
    [0.55, 0.55],
    [0.45, 0.6],
    [0.6, 0.4],
    [0.4, 0.45],
    [0.5, 0.7],
    [0.35, 0.65],
    [0.65, 0.6],
    [0.5, 0.35],
  ]
  // A click on a marker, label, popup or map control opens that instead of arming an incident
  // (on a phone the city's markers cover much of the map): only click open map.
  const onOpenMap = (x, y) =>
    page.evaluate(
      ([px, py]) =>
        !document
          .elementFromPoint(px, py)
          ?.closest(
            '.leaflet-marker-pane, .leaflet-popup-pane, .leaflet-tooltip-pane, ' +
              '.leaflet-control-container, .map-controls, .leaflet-interactive',
          ),
      [x, y],
    )
  let armed = false
  for (const [fx, fy] of offsets) {
    const x = box.x + box.width * fx
    const y = box.y + box.height * fy
    if (!(await onOpenMap(x, y))) continue
    await page.mouse.click(x, y)
    // isVisible() does not wait: wait for the form to re-render with the armed incident
    armed = await submit
      .waitFor({ state: 'visible', timeout: 1500 })
      .then(() => true)
      .catch(() => false)
    if (armed) break
  }
  if (!armed) throw new Error('map click did not arm a pending incident')
  await submit.click({ timeout: 5000 })
  await page.waitForSelector(`${TOAST_REGION} >> text=/dispatched/i`, { timeout: 8000 })
  await page.waitForTimeout(250) // entry transition, if any
}

// ---------------------------------------------------------------- main
async function main() {
  const o = parseArgs(process.argv.slice(2))
  REAL_TILES = o.realTiles
  mkdirSync(o.out, { recursive: true })
  const server = o.url ? null : await startPreview()
  const base = o.url ?? server.base
  const { browser, channel } = await launch(o)
  const mockAll = !o.url || o.mockApi
  const mode = o.url
    ? `${base} (${mockAll ? 'API mocked' : 'live API, mutations mocked'})`
    : `vite preview ${base} (API mocked)`
  console.log(`clickability: ${mode} · browser ${channel}`)
  const results = []
  /** Content-Security-Policy violations seen in any page (fonts, scripts, ...). */
  const cspViolations = new Set()
  /** States skipped because the live data lacks them (live mode only). */
  const skipped = []
  const t0 = Date.now()

  const record = (city, vp, state, res, shot) => {
    results.push({
      city,
      viewport: vp,
      state,
      checked: res.checked,
      layoutChecked: res.layoutChecked ?? 0,
      failures: res.failures,
      screenshot: shot,
    })
    const mark = res.failures.length ? `FAIL ${res.failures.length}` : 'ok'
    console.log(
      `  ${city.padEnd(9)} ${vp.padEnd(9)} ${state.padEnd(26)} ${String(res.checked).padStart(3)} controls ${String(res.layoutChecked ?? 0).padStart(4)} boxes  ${mark}`,
    )
    for (const f of res.failures.slice(0, 6))
      console.log(
        `      - ${f.control} ${f.reason} at (${f.x},${f.y})${f.hit ? ` by ${f.hit}` : ''}`,
      )
    if (res.failures.length > 6) console.log(`      ... ${res.failures.length - 6} more`)
  }
  const fail = (city, vp, state, err) =>
    record(city, vp, state, {
      checked: 0,
      failures: [
        {
          control: '(scenario)',
          reason: `error: ${String(err.message ?? err).split('\n')[0]}`,
          x: 0,
          y: 0,
          hit: '-',
        },
      ],
    })

  try {
    for (const vp of o.viewports) {
      const [w, h] = vp.split('x').map(Number)
      const context = await browser.newContext({
        viewport: { width: w, height: h },
        deviceScaleFactor: 1,
        reducedMotion: 'reduce',
        colorScheme: o.dark ? 'dark' : 'light',
      })
      await context.route(/tile\.openstreetmap\.org|basemaps\.cartocdn\.com/, (route) =>
        o.realTiles
          ? route.continue()
          : route.fulfill({ status: 200, contentType: 'image/png', body: TILE }),
      )
      await context.route(/\/(api\/v1\/|readyz)/, async (route) => {
        const req = route.request()
        const res = answer(req.method(), req.url(), req.postData(), {
          mutationsOnly: !mockAll,
        })
        if (res) await route.fulfill(res)
        else await route.continue()
      })
      if (mockAll) await context.routeWebSocket(/\/api\/v1\/cities\/[^/]+\/stream/, streamHandler)

      for (const city of o.cities) {
        const page = await context.newPage()
        page.on('console', (m) => {
          if (m.type() === 'error' && /Content Security Policy/i.test(m.text()))
            cspViolations.add(m.text().slice(0, 200))
        })
        const shot = (state) => {
          const p = join(o.out, `${city}-${state}-${vp}.png`)
          return page.screenshot({ path: p }).then(() => p)
        }
        const go = async (view) => {
          await page.goto(`${base}/#/${view}/${city}`)
          await page.reload() // hash-only navigation does not reload; start every view clean
          await waitReady(page, view)
        }
        const run = async (state, fn) => {
          try {
            await fn()
          } catch (e) {
            if (e instanceof SkipState) {
              skipped.push(`${city} ${vp} ${state}: ${e.message}`)
              console.log(
                `  ${city.padEnd(9)} ${vp.padEnd(9)} ${state.padEnd(26)} skipped (${e.message})`,
              )
            } else fail(city, vp, state, e)
          }
        }
        /** Live data may lack a state the mocks always have: skip it there, never in mock mode. */
        const needs = async (locator, what) => {
          if (!mockAll && (await locator.count()) === 0)
            throw new SkipState(`no ${what} in the live data`)
        }

        await run('live', async () => {
          await go('live')
          const p = await shot('live')
          record(city, vp, 'live', await page.evaluate(hitTest, null), p)
        })
        await run('live+vehicle-why-route', async () => {
          // select the transporting ambulance: its card shows "Why this route" for the TRANSPORT leg
          const transporting = page.locator(
            'section[aria-labelledby="fleet-title"] button[aria-pressed]',
            { hasText: 'Transporting' },
          )
          await needs(transporting, 'transporting vehicle')
          await transporting.first().click({ timeout: 5000 })
          const why = page.locator('section[aria-labelledby="fleet-title"] summary').first()
          await why.waitFor({ timeout: 8000 })
          await why.click({ timeout: 5000 }) // open it (and hover: alternatives on the map)
          await page.waitForTimeout(900) // the map flies to the vehicle
          await settle(page)
          const p = await shot('live-vehicle')
          record(city, vp, 'live+vehicle-why-route', await page.evaluate(hitTest, null), p)
        })
        await run('live+signal-popup', async () => {
          await go('live')
          // a queued junction: its popup lists the claimant queue
          const queued = page.locator('.sig.is-queued')
          await needs(queued, 'queued junction')
          await queued.first().dispatchEvent('click')
          await page.waitForSelector('.leaflet-popup .sig-pop', { timeout: 5000 })
          await settle(page)
          const p = await shot('live-signal-popup')
          record(city, vp, 'live+signal-popup', await page.evaluate(hitTest, null), p)
          // close it like an operator would (the close button must be reachable), then start
          // the dispatch from a freshly fitted map (the popup may have auto-panned it)
          await page.locator('.leaflet-popup-close-button').first().click({ timeout: 5000 })
          await go('live')
        })
        await run('live+dispatch-toast', async () => {
          await dispatchFromMap(page)
          const p = await shot('live-toast')
          record(city, vp, 'live+dispatch-toast', await page.evaluate(hitTest, null), p)
        })
        await run('history+toast-after-dispatch', async () => {
          await page.getByRole('tab', { name: 'History' }).click({ timeout: 5000 })
          await waitReady(page, 'history')
          const p = await shot('history-after-dispatch')
          record(city, vp, 'history+toast-after-dispatch', await page.evaluate(hitTest, null), p)
        })
        await run('history', async () => {
          await go('history')
          const p = await shot('history')
          record(city, vp, 'history', await page.evaluate(hitTest, null), p)
        })
        await run('history+apikey-popover', async () => {
          await page.locator('header button[aria-expanded]').first().click({ timeout: 5000 })
          await page.waitForSelector('header form', { timeout: 3000 })
          await settle(page)
          record(
            city,
            vp,
            'history+apikey-popover',
            await page.evaluate(hitTest, ['header form']),
            await shot('apikey'),
          )
          await page.locator('header form button[type="submit"]').click({ timeout: 5000 })
          await page.waitForSelector(`${TOAST_REGION} >> text=/API key/i`, { timeout: 5000 })
          await page.waitForTimeout(250)
          const p = await shot('history-toast')
          record(city, vp, 'history+toast', await page.evaluate(hitTest, null), p)
        })
        await run('history+drawer', async () => {
          await go('history')
          await page.getByRole('button', { name: 'Details' }).first().click({ timeout: 5000 })
          await page.waitForSelector('[role="dialog"]', { timeout: 5000 })
          await page.waitForFunction(
            () =>
              !/Loading/.test(document.querySelector('[role="dialog"]')?.textContent ?? 'Loading'),
            null,
            { timeout: 10_000 },
          )
          await settle(page)
          const p = await shot('history-drawer')
          record(
            city,
            vp,
            'history+drawer',
            await page.evaluate(hitTest, ['[role="dialog"]', TOAST_REGION]),
            p,
          )
        })
        await run('history+drawer-why-route', async () => {
          // the drawer of the first (MEDICAL, transporting) incident: open a "Why this route"
          const whys = page.locator('[role="dialog"] summary', { hasText: 'Why this route' })
          await needs(whys, 'route choice in this incident')
          const why = whys.first()
          await why.click({ timeout: 5000 })
          await why.hover()
          await settle(page)
          const p = await shot('history-drawer-why')
          record(
            city,
            vp,
            'history+drawer-why-route',
            await page.evaluate(hitTest, ['[role="dialog"]', TOAST_REGION]),
            p,
          )
        })
        await run('analytics', async () => {
          await go('analytics')
          const p = await shot('analytics')
          record(city, vp, 'analytics', await page.evaluate(hitTest, null), p)
        })
        await page.close()
      }
      await context.close()
    }
  } finally {
    await browser.close()
    server?.stop()
  }

  const failed = results.filter((r) => r.failures.length > 0)
  const summary = {
    mode,
    browser: channel,
    states: results.length,
    failedStates: failed.length,
    controlsChecked: results.reduce((n, r) => n + r.checked, 0),
    layoutBoxesChecked: results.reduce((n, r) => n + r.layoutChecked, 0),
    failures: results.reduce((n, r) => n + r.failures.length, 0),
    seconds: Math.round((Date.now() - t0) / 1000),
    cspViolations: [...cspViolations],
    skipped,
  }
  writeFileSync(join(o.out, 'results.json'), JSON.stringify({ summary, results }, null, 2))
  console.log(
    `\n${summary.states} states, ${summary.controlsChecked} control checks, ` +
      `${summary.layoutBoxesChecked} layout box checks, ` +
      `${summary.failures} failures in ${summary.failedStates} states (${summary.seconds} s). ` +
      `Screenshots + results.json in ${o.out}`,
  )
  if (skipped.length > 0) {
    console.log(`Skipped, state not in the live data (${skipped.length}):`)
    for (const s of skipped) console.log(`  - ${s}`)
  }
  if (cspViolations.size > 0) {
    console.log(`CSP violations (${cspViolations.size}):`)
    for (const v of cspViolations) console.log(`  - ${v}`)
    if (!o.keepGoing) process.exitCode = 1
  } else console.log('No Content-Security-Policy violations.')
  if (failed.length > 0) {
    const byVp = {}
    for (const r of failed) byVp[r.viewport] = (byVp[r.viewport] ?? 0) + 1
    console.log(`failing states by viewport: ${JSON.stringify(byVp)}`)
    if (!o.keepGoing) process.exitCode = 1
  }
}

main().catch((e) => {
  console.error(e)
  process.exitCode = 2
})
