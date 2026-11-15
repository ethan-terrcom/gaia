import { z } from 'zod'

const errorEnvelopeSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    request_id: z.string().optional(),
  }),
})

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string | undefined

  constructor(
    status: number,
    code: string,
    message: string,
    requestId?: string,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
  }
}

export function parseErrorEnvelope(status: number, body: unknown): ApiError {
  const parsed = errorEnvelopeSchema.safeParse(body)
  if (parsed.success) {
    return new ApiError(
      status,
      parsed.data.error.code,
      parsed.data.error.message,
      parsed.data.error.request_id,
    )
  }
  return new ApiError(
    status,
    'http_error',
    'the request could not be completed',
  )
}
