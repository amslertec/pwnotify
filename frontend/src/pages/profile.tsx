import { useTranslation } from 'react-i18next'

import { AccountTab } from '@/components/settings/account-tab'
import { PageHeader } from '@/components/page-header'
import { useAuth } from '@/lib/auth'

export default function ProfilePage() {
  const { t } = useTranslation()
  const { user } = useAuth()
  const name = user?.display_name || user?.username || ''
  const ssoSuffix = user?.is_sso ? t('profile.ssoSuffix') : ''
  return (
    <div>
      <PageHeader
        title={t('profile.title')}
        description={t('profile.subtitle', { name, sso: ssoSuffix })}
      />
      <AccountTab />
    </div>
  )
}
