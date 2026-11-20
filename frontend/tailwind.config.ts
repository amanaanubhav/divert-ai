import type { Config } from 'tailwindcss'
import plugin from 'tailwindcss/plugin'
import { cssVariables, DARK, LIGHT, mapVariables, tokenName } from './src/lib/palette'
import { Z_INDEX } from './src/lib/zIndex'

/** `rgb(var(--c-x) / <alpha-value>)` so opacity modifiers (`bg-fg/10`) keep working. */
const token = (key: string) => `rgb(var(--c-${tokenName(key)}) / <alpha-value>)`

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        // Self-hosted Noto Sans (@fontsource/noto-sans 400/500/600, imported in main.tsx).
        sans: ['"Noto Sans"', '"Segoe UI"', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
        mono: ['ui-monospace', '"Cascadia Mono"', 'Consolas', '"Liberation Mono"', 'monospace'],
      },
      colors: {
        page: token('page'),
        surface: token('surface'),
        subtle: token('subtle'),
        line: token('line'),
        field: token('field'),
        fg: token('fg'),
        muted: token('muted'),
        primary: {
          DEFAULT: token('primary'),
          hover: token('primaryHover'),
          active: token('primaryActive'),
          subtle: token('primarySubtle'),
        },
        'on-primary': token('onPrimary'),
        accent: token('accent'),
        ok: { DEFAULT: token('ok'), subtle: token('okSubtle') },
        warn: { DEFAULT: token('warn'), subtle: token('warnSubtle') },
        bad: { DEFAULT: token('bad'), subtle: token('badSubtle') },
        info: { DEFAULT: token('info'), subtle: token('infoSubtle') },
        scrim: token('scrim'),
      },
      zIndex: Object.fromEntries(Object.entries(Z_INDEX).map(([k, v]) => [k, String(v)])),
      // Corners: `rounded` (4 px) everywhere; `rounded-full` only for status dots.
      boxShadow: {
        // At most a subtle 1 px shadow (popovers, drawer, toasts); panels rely on their border.
        DEFAULT: '0 1px 2px rgb(0 0 0 / 0.12)',
      },
    },
  },
  plugins: [
    plugin(({ addBase }) => {
      addBase({
        ':root': { ...cssVariables(LIGHT), ...mapVariables(), colorScheme: 'light' },
        '@media (prefers-color-scheme: dark)': {
          ':root': { ...cssVariables(DARK), colorScheme: 'dark' },
        },
      })
    }),
  ],
} satisfies Config
