import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchPendingApprovals, fetchTodayCosts } from '../api/client'
import { PendingApprovalsBadge, TodaySpend } from './AppLayout'

vi.mock('../api/client', () => ({
  fetchPendingApprovals: vi.fn(),
  fetchTodayCosts: vi.fn(),
}))

function renderWithClient(ui: React.ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
  )
}

describe('PendingApprovalsBadge', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('shows the pending count', async () => {
    vi.mocked(fetchPendingApprovals).mockResolvedValue({
      items: [],
      total_pending: 3,
    })

    renderWithClient(<PendingApprovalsBadge />)

    const badge = await screen.findByLabelText('3 pending approvals')
    expect(badge.textContent).toBe('3')
  })

  it('is hidden at zero', async () => {
    vi.mocked(fetchPendingApprovals).mockResolvedValue({
      items: [],
      total_pending: 0,
    })

    renderWithClient(<PendingApprovalsBadge />)

    await waitFor(() => {
      expect(screen.queryByLabelText(/pending approvals/)).toBeNull()
    })
  })

  it('caps the visible label at 99+', async () => {
    vi.mocked(fetchPendingApprovals).mockResolvedValue({
      items: [],
      total_pending: 120,
    })

    renderWithClient(<PendingApprovalsBadge />)

    const badge = await screen.findByLabelText('120 pending approvals')
    expect(badge.textContent).toBe('99+')
  })
})

describe('TodaySpend', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('formats the spend and budget', async () => {
    vi.mocked(fetchTodayCosts).mockResolvedValue({
      spent_usd: '1.23',
      budget_usd: '20.00',
      by_provider: [{ provider: 'deepseek', cost_usd: '1.23' }],
      day_start: '2026-01-01T00:00:00Z',
    })

    renderWithClient(<TodaySpend />)

    expect(await screen.findByText('$1.23 / $20.00')).toBeDefined()
  })

  it('turns amber at 80%', async () => {
    vi.mocked(fetchTodayCosts).mockResolvedValue({
      spent_usd: '16.00',
      budget_usd: '20.00',
      by_provider: [{ provider: 'deepseek', cost_usd: '16.00' }],
      day_start: '2026-01-01T00:00:00Z',
    })

    const { container } = renderWithClient(<TodaySpend />)

    await screen.findByText('$16.00 / $20.00')
    expect(container.innerHTML).toContain('--color-warning')
  })

  it('turns red at 100%', async () => {
    vi.mocked(fetchTodayCosts).mockResolvedValue({
      spent_usd: '20.00',
      budget_usd: '20.00',
      by_provider: [{ provider: 'deepseek', cost_usd: '20.00' }],
      day_start: '2026-01-01T00:00:00Z',
    })

    const { container } = renderWithClient(<TodaySpend />)

    await screen.findByText('$20.00 / $20.00')
    expect(container.innerHTML).toContain('--color-danger')
  })
})
