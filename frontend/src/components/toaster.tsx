import { Toaster as Sonner } from 'sonner'

import { useTheme } from './theme-provider'

export function Toaster() {
  const { resolved } = useTheme()
  return (
    <Sonner
      theme={resolved}
      position="bottom-right"
      toastOptions={{
        classNames: {
          toast: 'group rounded-lg border border-border bg-card text-card-foreground shadow-lg',
          description: 'text-muted-foreground',
        },
      }}
    />
  )
}
