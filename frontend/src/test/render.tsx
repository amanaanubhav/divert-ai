import { QueryClient } from '@tanstack/react-query'
import { render, type RenderResult } from '@testing-library/react'
import type { ReactElement } from 'react'
import { Providers } from '../app/providers'
import { Toaster } from '../components/ui/Toaster'

export function renderWithProviders(ui: ReactElement): RenderResult & { client: QueryClient } {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  })
  const result = render(
    <Providers client={client}>
      {ui}
      <Toaster />
    </Providers>,
  )
  return { ...result, client }
}
