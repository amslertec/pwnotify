import { AvatarImage } from '@/components/avatar-image'
import { useAuth } from '@/lib/auth'

/** Profilbild des angemeldeten Benutzers — hochgeladenes/Entra-Foto oder Initialen-Fallback. */
export function UserAvatar({ className }: { className?: string }) {
  const { user } = useAuth()
  const name = user?.display_name || user?.username || '?'

  return (
    <AvatarImage
      name={name}
      src={user?.has_avatar ? `/api/auth/me/avatar?v=${user.avatar_version}` : undefined}
      className={className}
    />
  )
}
