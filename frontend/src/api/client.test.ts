import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from './client'
import { ApiError } from './errors'
import { TOKEN_KEY } from './token'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fetchMock(
  handler: (request: Request) => Response | Promise<Response>,
): typeof fetch {
  return vi.fn(async (input: Request | URL | string) => {
    const request =
      input instanceof Request ? input : new Request(input as string)
    return handler(request)
  }) as unknown as typeof fetch
}

describe('api client middleware', () => {
  beforeEach(() => {
    sessionStorage.clear()
  })

  it('adds the bearer token when one exists', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'token-abc')
    let authorization = ''
    const fetch = fetchMock((request) => {
      authorization = request.headers.get('Authorization') ?? ''
      return jsonResponse(200, { user_id: 'user-1', workspace_id: 'ws-1' })
    })

    const response = await api.GET('/api/v1/me', { fetch })

    expect(authorization).toBe('Bearer token-abc')
    expect(response.data).toEqual({ user_id: 'user-1', workspace_id: 'ws-1' })
  })

  it('clears the token and fires gaia:unauthorized on a 401', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'token-abc')
    const unauthorized = vi.fn()
    window.addEventListener('gaia:unauthorized', unauthorized)
    const fetch = fetchMock(() =>
      jsonResponse(401, {
        error: { code: 'unauthorized', message: 'a bearer token is required' },
      }),
    )

    await expect(api.GET('/api/v1/me', { fetch })).rejects.toBeInstanceOf(
      ApiError,
    )

    expect(sessionStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(unauthorized).toHaveBeenCalled()
  })

  it('maps a validation envelope to an ApiError', async () => {
    const fetch = fetchMock(() =>
      jsonResponse(422, {
        error: { code: 'validation_error', message: 'limit: invalid value' },
      }),
    )

    const error = await api
      .GET('/api/v1/approvals', { fetch })
      .catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(422)
    expect((error as ApiError).code).toBe('validation_error')
    expect((error as ApiError).message).toBe('limit: invalid value')
  })

  it('maps a non-JSON 500 to a generic ApiError', async () => {
    const fetch = fetchMock(() => new Response('boom', { status: 500 }))

    const error = await api
      .GET('/api/v1/me', { fetch })
      .catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(500)
    expect((error as ApiError).code).toBe('http_error')
    expect((error as ApiError).message).toBe(
      'the request could not be completed',
    )
  })
})
