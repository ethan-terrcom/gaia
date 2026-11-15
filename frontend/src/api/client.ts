import createClient from 'openapi-fetch'
import { parseErrorEnvelope } from './errors'
import type { paths } from './schema'
import { clearToken, getToken } from './token'

export const api = createClient<paths>({ baseUrl: '' })

api.use({
  onRequest({ request }) {
    const token = getToken()
    if (token) {
      request.headers.set('Authorization', `Bearer ${token}`)
    }
  },
  async onResponse({ response }) {
    if (response.status === 401) {
      clearToken()
      window.dispatchEvent(new Event('gaia:unauthorized'))
    }
    if (!response.ok) {
      const body: unknown = await response
        .clone()
        .json()
        .catch(() => undefined)
      throw parseErrorEnvelope(response.status, body)
    }
  },
})

export async function fetchMe() {
  const response = await api.GET('/api/v1/me')
  return response.data
}

export async function fetchPendingApprovals() {
  // The badge needs only ``total_pending``; one row keeps the response tiny.
  const response = await api.GET('/api/v1/approvals', {
    params: { query: { status: 'pending', limit: 1 } },
  })
  if (response.error) {
    throw response.error
  }
  return response.data
}

export async function fetchTodayCosts() {
  const response = await api.GET('/api/v1/costs/today')
  return response.data
}
