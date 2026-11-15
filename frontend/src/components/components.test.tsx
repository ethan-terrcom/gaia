import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/errors'
import { ConfirmDialog } from './ConfirmDialog'
import { EmptyState, ErrorState, LoadingState } from './States'
import { StatusBadge } from './StatusBadge'
import { toneOf } from './status'
import { formatRelative, formatUsd, shortSha } from './format'

describe('States', () => {
  it('renders loading, empty and error states', () => {
    render(<LoadingState />)
    expect(screen.getByRole('status').textContent).toContain('Loading')

    render(<EmptyState title="No PRs yet">Start a run.</EmptyState>)
    expect(screen.getByText('No PRs yet')).toBeTruthy()

    const retry = vi.fn()
    render(
      <ErrorState
        error={new ApiError(500, 'boom_code', 'It broke')}
        onRetry={retry}
      />,
    )
    expect(screen.getByRole('alert').textContent).toContain('It broke')
    expect(screen.getByText('boom_code')).toBeTruthy()
    fireEvent.click(screen.getByText('Retry'))
    expect(retry).toHaveBeenCalled()
  })

  it('shows a generic message for non-API errors', () => {
    render(<ErrorState error={new Error('secret detail')} />)
    expect(screen.getByRole('alert').textContent).not.toContain('secret detail')
  })
})

describe('ConfirmDialog', () => {
  it('requires the exact phrase before confirming', () => {
    const confirm = vi.fn()
    render(
      <ConfirmDialog
        open
        title="Merge PR #7?"
        requireText="merge 7"
        onConfirm={confirm}
        onCancel={() => {}}
      />,
    )
    const button = screen.getByText('Confirm') as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'merge 8' },
    })
    expect(button.disabled).toBe(true)
    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'merge 7' },
    })
    expect(button.disabled).toBe(false)
    fireEvent.click(button)
    expect(confirm).toHaveBeenCalled()
  })

  it('cancels on Escape and renders nothing when closed', () => {
    const cancel = vi.fn()
    const { rerender } = render(
      <ConfirmDialog
        open
        title="Remove?"
        onConfirm={() => {}}
        onCancel={cancel}
      />,
    )
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(cancel).toHaveBeenCalled()
    rerender(
      <ConfirmDialog
        open={false}
        title="Remove?"
        onConfirm={() => {}}
        onCancel={cancel}
      />,
    )
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('format', () => {
  it('formats money, times and SHAs', () => {
    expect(formatUsd('1.234')).toBe('$1.23')
    expect(formatUsd('0.004')).toBe('<$0.01')
    expect(formatUsd(null)).toBe('$0.00')
    expect(formatUsd('nope')).toBe('$—')
    const now = new Date('2026-11-14T12:00:00Z')
    expect(formatRelative('2026-11-14T09:00:00Z', now)).toBe('3 hours ago')
    expect(formatRelative('2026-11-14T11:59:50Z', now)).toBe('just now')
    expect(formatRelative(null, now)).toBe('—')
    expect(formatRelative('garbage', now)).toBe('—')
    expect(shortSha('abcdef0123456789')).toBe('abcdef01')
    expect(shortSha(null)).toBe('—')
  })
})

describe('StatusBadge', () => {
  it('maps known statuses to tones and tolerates unknown ones', () => {
    expect(toneOf('failed')).toBe('bad')
    expect(toneOf('brand_new_status')).toBe('neutral')
    expect(toneOf(null)).toBe('neutral')
    render(<StatusBadge status="changes_requested" />)
    expect(screen.getByText('changes requested')).toBeTruthy()
  })
})
