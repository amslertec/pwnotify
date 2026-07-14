import { QueryClient } from '@tanstack/react-query'

import { ApiError } from './api'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: (count, error) => {
        // 4xx nicht wiederholen
        if (error instanceof ApiError && error.status < 500) return false
        return count < 2
      },
      refetchOnWindowFocus: false,
    },
  },
})
