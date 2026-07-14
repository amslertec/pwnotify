import { Loader2 } from 'lucide-react'

export function FullScreenLoader() {
  return (
    <div className="grid h-full place-items-center">
      <Loader2 className="text-primary size-6 animate-spin" />
    </div>
  )
}
