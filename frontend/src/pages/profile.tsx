import { AccountTab } from '@/components/settings/account-tab'
import { PageHeader } from '@/components/page-header'
import { useAuth } from '@/lib/auth'

export default function ProfilePage() {
  const { user } = useAuth()
  const name = user?.display_name || user?.username || ''
  return (
    <div>
      <PageHeader
        title="Mein Konto"
        description={`Angemeldet als ${name}${user?.is_sso ? ' (SSO)' : ''} · aktive Sitzungen verwalten.`}
      />
      <AccountTab />
    </div>
  )
}
