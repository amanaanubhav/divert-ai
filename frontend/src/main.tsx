import 'leaflet/dist/leaflet.css'
// Self-hosted Noto Sans (served from /assets, so the CSP's font-src 'self' holds). Each weight
// ships per-script subsets (Latin, Devanagari, ...); the browser fetches only what a page uses.
import '@fontsource/noto-sans/400.css'
import '@fontsource/noto-sans/500.css'
import '@fontsource/noto-sans/600.css'
import './index.css'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './app/App'
import { Providers } from './app/providers'

const root = document.getElementById('root')
if (!root) throw new Error('#root missing from index.html')

createRoot(root).render(
  <StrictMode>
    <Providers>
      <App />
    </Providers>
  </StrictMode>,
)
