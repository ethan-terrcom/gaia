import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import App from './App.tsx'
import { TOKEN_KEY } from './api/token'

describe('App', () => {
  beforeEach(() => {
    sessionStorage.clear()
    window.history.replaceState(null, '', '/')
  })

  it('shows the connect screen without a token', () => {
    render(<App />)

    expect(screen.getByRole('heading', { name: 'Gaia' })).toBeDefined()
    expect(screen.getByText('Run', { exact: false }).textContent).toContain(
      'gaia ui',
    )
  })

  it('renders the router when a token is present', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'token-abc')

    render(<App />)

    expect((await screen.findAllByText('Dashboard')).length).toBeGreaterThan(0)
  })

  it('returns to the connect screen on gaia:unauthorized', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'token-abc')

    render(<App />)
    await screen.findAllByText('Dashboard')

    window.dispatchEvent(new Event('gaia:unauthorized'))

    await waitFor(() => {
      expect(screen.getByText('Run', { exact: false }).textContent).toContain(
        'gaia ui',
      )
    })
  })
})
