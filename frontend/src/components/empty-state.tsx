import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
}: {
  icon: LucideIcon
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-16 text-center">
      <div className="relative">
        <div className="bg-primary/10 absolute inset-0 rounded-2xl blur-xl" />
        <div className="border-border bg-card relative grid size-14 place-items-center rounded-2xl border">
          <Icon className="text-primary size-6" />
        </div>
      </div>
      <h3 className="font-display text-lg font-semibold">{title}</h3>
      {description && <p className="text-muted-foreground max-w-sm text-sm">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}
