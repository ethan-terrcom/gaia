import { fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router'
import { describe, expect, it } from 'vitest'
import { useHotkeys } from './useHotkeys'

function Harness() {
  useHotkeys()
  const location = useLocation()
  return (
    <div>
      <div data-testid="pathname">{location.pathname}</div>
      <input data-search-input aria-label="Search" />
      <input aria-label="Other" />
    </div>
  )
}

function renderRouter() {
  const router = createMemoryRouter(
    [
      { path: '/', element: <Harness /> },
      { path: '/approvals', element: <Harness /> },
    ],
    { initialEntries: ['/'] },
  )
  return render(<RouterProvider router={router} />)
}

describe('useHotkeys', () => {
  it('navigates with g then a', async () => {
    renderRouter()

    fireEvent.keyDown(window, { key: 'g' })
    fireEvent.keyDown(window, { key: 'a' })

    expect((await screen.findByTestId('pathname')).textContent).toBe(
      '/approvals',
    )
  })

  it('is ignored inside an input', () => {
    renderRouter()

    const input = screen.getByLabelText('Other')
    fireEvent.keyDown(input, { key: 'g' })
    fireEvent.keyDown(input, { key: 'a' })

    expect(screen.getByTestId('pathname').textContent).toBe('/')
  })

  it('focuses the first search input on /', () => {
    renderRouter()

    fireEvent.keyDown(window, { key: '/' })

    expect(document.activeElement).toBe(screen.getByLabelText('Search'))
  })
})
