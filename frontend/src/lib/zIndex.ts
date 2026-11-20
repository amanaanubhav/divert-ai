/**
 * The one z-index scale of the app (Tailwind: `z-map`, `z-map-overlay`, ...). Lower layers
 * never cover higher ones:
 *
 *   map < map overlays < sticky < header < popover < drawer < toast < skip link
 *
 * Every Leaflet map container is `isolation: isolate` (index.css), so Leaflet's own z-indexes
 * (panes 200-700, controls 800-1000) stay inside the map and never reach the header or a
 * drawer. Use these names instead of numeric `z-[…]` classes.
 *
 * Keep this file free of DOM and React imports: the Tailwind config (Node) imports it too.
 */
export const Z_INDEX = {
  /** Map containers (a stacking context each). */
  map: 0,
  /** Layer toggles and legend drawn over a map. */
  'map-overlay': 10,
  /** Sticky table headers inside scroll areas. */
  sticky: 20,
  /** App header: tabs, city switcher, status (and its popovers inside it). */
  header: 100,
  /** Popovers opened from the header (API key). */
  popover: 200,
  /** Modal incident drawer and its backdrop. */
  drawer: 300,
  /** Notifications. They let clicks through except on their own close button. */
  toast: 400,
  /** The focused "Skip to content" link. */
  skip: 500,
} as const

export type ZLayer = keyof typeof Z_INDEX
