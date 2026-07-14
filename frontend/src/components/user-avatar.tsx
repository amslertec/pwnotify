import { useAuth } from '@/lib/auth'
import { cn, initials } from '@/lib/utils'

/** Profilbild des angemeldeten Benutzers — hochgeladenes/Entra-Foto oder Initialen-Fallback. */
export function UserAvatar({ className }: { className?: string }) {
  const { user } = useAuth()
  const name = user?.display_name || user?.username || '?'

  if (user?.has_avatar) {
    return (
      <img
        src={`/api/auth/me/avatar?v=${user.avatar_version}`}
        alt={name}
        className={cn('rounded-full object-cover', className)}
      />
    )
  }

  return (
    <span
      className={cn(
        'bg-primary/15 text-primary grid place-items-center rounded-full font-semibold',
        className,
      )}
    >
      {initials(name)}
    </span>
  )
}
