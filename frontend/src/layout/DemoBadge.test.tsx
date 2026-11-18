import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchMe } from '../api/client'
import { DemoBadge } from './DemoBadge'

vi.mock('../api/client', () => ({
  fetchMe: vi.fn(),
}))

function renderBadge() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <DemoBadge />
    </QueryClientProvider>,
  )
}

describe('DemoBadge', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('shows an amber demo mode badge with the env-var tooltip', async () => {
    vi.mocked(fetchMe).mockResolvedValue({
      user_id: 'user-1',
      workspace_id: 'ws-1',
      demo_mode: true,
    })

    renderBadge()

    const badge = await screen.findByText('Demo')
    expect(badge.getAttribute('title')).toBe(
      'GAIA_FAKE_LLM=1: no real model is called',
    )
  })

  it('renders nothing when demo mode is off', async () => {
    vi.mocked(fetchMe).mockResolvedValue({
      user_id: 'user-1',
      workspace_id: 'ws-1',
      demo_mode: false,
    })

    renderBadge()

    await waitFor(() => {
      expect(screen.queryByText('Demo')).toBeNull()
    })
  })
})
