/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// The dev server proxies the backend (REST + WebSocket) so the app always talks to its own
// origin; production nginx does the same (see nginx/default.conf.template).
// VITE_API_BASE_URL overrides both.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.DIVERT_DEV_API ?? 'http://localhost:8000'
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': { target, changeOrigin: true, ws: true },
        '/readyz': { target, changeOrigin: true },
        '/healthz': { target, changeOrigin: true },
      },
    },
    preview: { port: 4173 },
    build: {
      target: 'es2022',
      sourcemap: true,
      chunkSizeWarningLimit: 700,
      // Never inline fonts as data: URIs: the nginx CSP only allows font-src 'self'.
      assetsInlineLimit: (file: string) => (/\.(woff2?|ttf|otf)$/.test(file) ? false : undefined),
    },
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      css: false,
      restoreMocks: true,
      unstubGlobals: true,
      include: ['src/**/*.test.{ts,tsx}'],
    },
  }
})
