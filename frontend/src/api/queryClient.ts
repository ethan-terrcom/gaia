import { QueryClient } from '@tanstack/react-query'
import { ApiError } from './errors'

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.status < 500) {
            return false
          }
          return failureCount < 2
        },
      },
    },
  })
}
